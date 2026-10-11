#!/usr/bin/env python3
"""Check out a resolved upstream release, build it, and create a Windows ZIP."""

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def run(arguments, *, cwd=None, env=None, capture=False):
    print("+ " + " ".join(map(str, arguments)), flush=True)
    result = subprocess.run(list(map(str, arguments)), cwd=cwd, env=env, check=True,
                            text=capture, encoding="utf-8" if capture else None,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else None


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def apply_patch(source, patch, directory=None):
    arguments = ["--directory", directory.as_posix()] if directory else []
    forward = subprocess.run(["git", "apply", *arguments, "--check", str(patch)], cwd=source,
                             capture_output=True, text=True, encoding="utf-8")
    if forward.returncode == 0:
        run(["git", "apply", *arguments, str(patch)], cwd=source)
        return "applied"
    reverse = subprocess.run(["git", "apply", *arguments, "--reverse", "--check", str(patch)], cwd=source,
                             capture_output=True, text=True, encoding="utf-8")
    if reverse.returncode == 0:
        return "already-upstream"
    raise RuntimeError(f"Windows patch {patch.name} needs review for this release:\n{forward.stderr}")


def check_lock_change(before, after, patched_packages):
    """Allow only the explicitly patched registry packages to change their source."""
    original = tomllib.loads(before.decode("utf-8"))
    actual = tomllib.loads(after.decode("utf-8"))
    for package in original["package"]:
        if (package["name"], package["version"]) in patched_packages:
            package.pop("source", None)
            package.pop("checksum", None)
    if original != actual:
        raise RuntimeError("Applying dependency patches changed unrelated locked dependencies")


def create_zip(directory, output):
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(directory).as_posix())


def build(manifest, project_id, work, output):
    if os.name != "nt":
        raise RuntimeError("Release binaries must be built and smoke-tested on Windows")
    project = next(project for project in manifest["projects"] if project["id"] == project_id)
    source = work / project_id / "source"
    staging = work / project_id / "package"
    if source.exists() or staging.exists():
        raise RuntimeError(f"Use a fresh work directory; {source.parent} already contains a build")
    source.mkdir(parents=True)
    staging.mkdir(parents=True)
    run(["git", "init", "--quiet", source])
    run(["git", "config", "core.autocrlf", "false"], cwd=source)
    run(["git", "remote", "add", "origin", project["url"]], cwd=source)
    run(["git", "fetch", "--depth=1", "origin", "refs/tags/" + project["tag"]], cwd=source)
    actual = run(["git", "rev-parse", "FETCH_HEAD^{commit}"], cwd=source, capture=True)
    if actual != project["commit"]:
        raise RuntimeError(f"Upstream tag moved after resolution: expected {project['commit']}, got {actual}")
    run(["git", "checkout", "--quiet", "--detach", actual], cwd=source)

    patches = []
    for name in project["patches"]:
        path = ROOT / name
        patches.append({"file": name, "sha256": sha256(path), "status": apply_patch(source, path)})

    cargo_manifest = source / project["manifest"]
    if not cargo_manifest.exists() or not (source / "Cargo.lock").exists():
        raise RuntimeError("The release must contain the configured Cargo manifest and an upstream lockfile")
    original_lock = (source / "Cargo.lock").read_bytes()
    lock_hash = sha256(source / "Cargo.lock")
    target = manifest["target"]
    env = os.environ.copy()
    env["CARGO_TARGET_DIR"] = str(work / project_id / "target")
    assets = staging / "share" / project_id / "assets"
    assets.mkdir(parents=True)
    env["ASSET_OUT_DIR"] = str(assets)
    metadata_args = ["--manifest-path", str(cargo_manifest), "--filter-platform", target,
                     "--no-default-features", "--features", ",".join(project["features"])]
    metadata = json.loads(run(["cargo", "metadata", "--format-version=1", "--locked", *metadata_args],
                              cwd=source, env=env, capture=True))
    overrides = []
    patched_packages = set()
    # Native Windows GnuPG supplies Windows paths. The locked gpg-agent crate
    # otherwise invokes MSYS2's cygpath even for drive-letter paths.
    for dependency in metadata["packages"]:
        if dependency["name"] != "sequoia-gpg-agent":
            continue
        version = dependency["version"]
        destination = source / "vendor" / ("sequoia-gpg-agent-" + version)
        shutil.copytree(Path(dependency["manifest_path"]).parent, destination)
        path = ROOT / "patches" / "gpg-agent-windows.patch"
        status = apply_patch(source, path, destination.relative_to(source))
        patches.append({"file": "patches/gpg-agent-windows.patch", "sha256": sha256(path),
                        "dependency": "sequoia-gpg-agent", "version": version, "status": status})
        key = "patch.crates-io.sequoia-gpg-agent-" + version.replace(".", "-")
        overrides.extend(["--config", key + '.package="sequoia-gpg-agent"',
                          "--config", key + ".path=" + json.dumps(destination.as_posix())])
        patched_packages.add((dependency["name"], version))
    if overrides:
        metadata = json.loads(run(["cargo", "metadata", "--offline", "--format-version=1",
                                  *overrides, *metadata_args], cwd=source, env=env, capture=True))
        check_lock_change(original_lock, (source / "Cargo.lock").read_bytes(), patched_packages)
    build_lock_hash = sha256(source / "Cargo.lock")
    base = ["--manifest-path", str(cargo_manifest), "--locked", "--target", target,
            "--no-default-features", "--features", ",".join(project["features"])]
    command = ["cargo", "build", "--release", "--package", project["package"], *overrides, *base]
    for name in project["binaries"]:
        command.extend(["--bin", name])
    run(command, cwd=source, env=env)
    if sha256(source / "Cargo.lock") != build_lock_hash:
        raise RuntimeError("The build changed its locked dependencies")
    packages = [package for package in metadata["packages"] if package["name"] == project["package"]]
    if len(packages) != 1:
        raise RuntimeError("Could not identify the built Cargo package")
    package = packages[0]
    # The WOT tag versions the library; its tools package has a separate version.
    if project_id != "wot" and package["version"] != project["version"]:
        raise RuntimeError(f"Tag/package version mismatch: {project['version']} vs {package['version']}")

    binaries = staging / "bin"
    binaries.mkdir()
    redist = Path(env["SEQUOIA_VC_REDIST_DIR"])
    runtime_hashes = {}
    for path in redist.glob("*.dll"):
        shutil.copy2(path, binaries / path.name)
        runtime_hashes[path.name] = sha256(binaries / path.name)
    if "vcruntime140.dll" not in runtime_hashes:
        raise RuntimeError("The Microsoft runtime was not staged")
    hashes = {}
    for name, arguments in project["binaries"].items():
        filename = name + ".exe"
        shutil.copy2(Path(env["CARGO_TARGET_DIR"]) / target / "release" / filename, binaries / filename)
        smoke_env = env | {"PATH": str(binaries) + os.pathsep + str(Path(env["SYSTEMROOT"]) / "System32")}
        run([binaries / filename, *arguments], cwd=staging, env=smoke_env)
        hashes[filename] = sha256(binaries / filename)

    notices = staging / "share" / project_id
    license_count = 0
    for folder in {source, cargo_manifest.parent}:
        for path in folder.iterdir():
            if path.is_file() and (path.name.upper().startswith(("LICENSE", "COPYING", "AUTHORS"))):
                shutil.copy2(path, notices / path.name)
                license_count += 1
    if not license_count:
        raise RuntimeError("No upstream license file found")
    shutil.copy2(source / "Cargo.lock", notices / "Cargo.lock")
    (notices / "Cargo.upstream.lock").write_bytes(original_lock)
    for patch in patches:
        patchdir = notices / "patches"
        patchdir.mkdir(exist_ok=True)
        shutil.copy2(ROOT / patch["file"], patchdir / Path(patch["file"]).name)
    # Include the corresponding upstream source with the exact commit and patches.
    tar = work / project_id / "source.tar"
    run(["git", "archive", "--format=tar", "--output", tar, actual], cwd=source)
    with tar.open("rb") as stream, (notices / "upstream-source.tar.gz").open("wb") as compressed:
        with gzip.GzipFile(filename="", mode="wb", fileobj=compressed, mtime=0) as archive:
            shutil.copyfileobj(stream, archive)
    if patched_packages:
        with tarfile.open(notices / "patched-dependency-sources.tar.gz", "w:gz") as archive:
            archive.add(source / "vendor", arcname="vendor")
    dependencies = [{key: dep.get(key) for key in ("name", "version", "license", "source", "repository")}
                    for dep in metadata["packages"]]
    (notices / "dependencies.json").write_text(json.dumps(dependencies, indent=2) + "\n", encoding="utf-8")
    record = project | {
        "target": target, "package_version": package["version"], "patches": patches,
        "pipeline_commit": manifest["pipeline_commit"], "release_tag": manifest["release_tag"],
        "rustc": run(["rustc", "--version", "--verbose"], capture=True),
        "cargo": run(["cargo", "--version"], capture=True), "upstream_lockfile_sha256": lock_hash,
        "build_lockfile_sha256": build_lock_hash, "binaries_sha256": hashes,
        "runtime_sha256": runtime_hashes,
    }
    (notices / "build.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    filename = f"{project['repository']}-{project['version']}-windows-x86_64.zip"
    create_zip(staging, output / filename)
    print(f"Created {output / filename}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, default=ROOT / "sources.json")
    parser.add_argument("--project", required=True)
    parser.add_argument("--work", type=Path, default=ROOT / ".build")
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    build(json.loads(args.sources.read_text(encoding="utf-8")), args.project,
          args.work.resolve(), args.output.resolve())


if __name__ == "__main__":
    main()
