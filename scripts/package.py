#!/usr/bin/env python3
"""Validate all component artifacts before making the complete download bundle."""

import argparse
import json
from pathlib import Path, PurePosixPath
import shutil
import zipfile

from build_project import ROOT, create_zip, sha256


def extract_checked(archive, staging):
    for info in archive.infolist():
        path = PurePosixPath(info.filename)
        if path.is_absolute() or ".." in path.parts or "\\" in info.filename or ":" in info.filename:
            raise ValueError(f"Unsafe archive member: {info.filename}")
        if info.is_dir():
            continue
        destination = staging.joinpath(*path.parts)
        data = archive.read(info)
        if destination.exists():
            if destination.read_bytes() != data:
                raise ValueError(f"Conflicting packaged files: {info.filename}")
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)


def collect(manifest, artifacts, staging, output):
    staging.mkdir(parents=True, exist_ok=False)
    output.mkdir(parents=True, exist_ok=True)
    archives = list(artifacts.rglob("*.zip"))
    expected = {f"{project['repository']}-{project['version']}-windows-x86_64.zip": project
                for project in manifest["projects"]}
    if len(archives) != len(expected) or {archive.name for archive in archives} != set(expected):
        raise ValueError("Missing, duplicated or unexpected component ZIPs; refusing a partial release")
    records = []
    for path in sorted(archives):
        project = expected[path.name]
        with zipfile.ZipFile(path) as archive:
            record = json.loads(archive.read(f"share/{project['id']}/build.json"))
            for key in ("id", "commit", "tag", "version"):
                if record[key] != project[key]:
                    raise ValueError(f"Artifact source mismatch for {project['id']}: {key}")
            for key in ("target", "pipeline_commit", "release_tag"):
                if record[key] != manifest[key]:
                    raise ValueError(f"Artifact build mismatch for {project['id']}: {key}")
            if set(record["binaries_sha256"]) != {name + ".exe" for name in project["binaries"]}:
                raise ValueError(f"Unexpected binaries in {path.name}")
            extract_checked(archive, staging)
            for filename, digest in (record["binaries_sha256"] | record["runtime_sha256"]).items():
                if sha256(staging / "bin" / filename) != digest:
                    raise ValueError(f"Binary checksum mismatch: {filename}")
            records.append(record)
        shutil.copy2(path, output / path.name)
    expected_binaries = {name + ".exe" for project in manifest["projects"] for name in project["binaries"]}
    if {path.name for path in (staging / "bin").glob("*.exe")} != expected_binaries:
        raise ValueError("The complete bundle does not contain exactly the expected utilities")
    report = manifest | {"builds": records}
    (staging / "sources.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    shutil.copy2(ROOT / "WINDOWS-README.txt", staging / "README.txt")
    return report


def finish(report, staging, output):
    filename = "sequoia-all-windows-x86_64.zip"
    create_zip(staging, output / filename)
    (output / "sources.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    binary_count = sum(len(project["binaries"]) for project in report["projects"])
    notes = ["Native Windows x64 builds of the latest stable Sequoia utility releases.", "",
             f"Download `sequoia-all-windows-x86_64.zip` for all {binary_count} executables, or a component ZIP.", "",
             "| Project | Upstream tag | Commit |", "| --- | --- | --- |"]
    for project in report["projects"]:
        notes.append(f"| {project['repository']} | {project['tag']} | [{project['commit'][:12]}]({project['source_url']}) |")
    notes += ["", "Built with Windows CNG. OpenSSL is linked statically; Microsoft runtime DLLs are included.",
              "Chameleon and its GnuPG-agent dependency include the documented Windows compatibility patches.",
              "`gpg-sq` secret-key operations require a separately installed native GnuPG (`gpg-agent` and `gpgconf`).",
              "`sq-cryptoki` token operations require a separately installed vendor PKCS#11 module and device.",
              "Foreground Parcimonie is supported; Unix daemonization is unavailable on Windows.",
              "", "The bundles include upstream source, applied patches, licenses, Cargo lockfiles and build provenance.",
              "SHA256 checksums are in `SHA256SUMS`. Binaries are not Authenticode-signed."]
    (output / "release-notes.md").write_text("\n".join(notes) + "\n", encoding="utf-8")
    checksums = [f"{sha256(path)}  {path.name}" for path in sorted(output.iterdir())
                 if path.is_file() and path.name != "SHA256SUMS"]
    (output / "SHA256SUMS").write_text("\n".join(checksums) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, default=ROOT / "sources.json")
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--staging", type=Path, default=ROOT / ".build" / "complete")
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--skip-smoke", action="store_true", help="Only for packaging tests; never used in release CI")
    args = parser.parse_args()
    manifest = json.loads(args.sources.read_text(encoding="utf-8"))
    staging, output = args.staging.resolve(), args.output.resolve()
    report = collect(manifest, args.artifacts.resolve(), staging, output)
    if not args.skip_smoke:
        import os
        from smoke import smoke
        gnupg = os.environ.get("SEQUOIA_GNUPG_BIN")
        if not gnupg:
            parser.error("SEQUOIA_GNUPG_BIN is required for release smoke tests")
        smoke(staging / "bin", Path(gnupg))
    finish(report, staging, output)


if __name__ == "__main__":
    main()
