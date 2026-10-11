"""Verify complete release assembly and publication with synthetic Windows artifacts."""

import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
import urllib.error
import zipfile


ROOT = Path(__file__).resolve().parents[1]
with mock.patch.object(sys, "path", [str(ROOT / "scripts"), *sys.path]):
    import package as packager
    import publish as publisher
    import release_status

PIPELINE_COMMIT = "c" * 40
RUNTIME = {
    "vcruntime140.dll": b"MZ\x90\x00Synthetic Microsoft VC runtime\r\n",
    "msvcp140.dll": b"MZ\x90\x00Synthetic Microsoft C++ runtime\r\n",
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def sources_manifest():
    projects = json.loads((ROOT / "projects.json").read_text())
    for index, project in enumerate(projects, 1):
        project["version"] = f"1.{index}.0"
        project["tag"] = ("server/v" if project["id"] == "keystore" else "v") + project["version"]
        project["commit"] = f"{index:040x}"
        base = "https://gitlab.com/sequoia-pgp/" + project["repository"]
        project["url"] = base + ".git"
        project["source_url"] = base + "/-/tree/" + project["commit"]
    return {"schema": 1, "target": "x86_64-pc-windows-msvc",
            "pipeline_commit": PIPELINE_COMMIT, "release_tag": "windows-0123456789abcdef",
            "projects": projects}


def component_name(project):
    return f"{project['repository']}-{project['version']}-windows-x86_64.zip"


def download_names(manifest):
    return {component_name(project) for project in manifest["projects"]} | {
        "sequoia-all-windows-x86_64.zip", "sources.json", "SHA256SUMS", "release-notes.md"}


def source_archive(project):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        content = ("Synthetic source for " + project["repository"] + "\n").encode()
        info = tarfile.TarInfo("README.md")
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def write_component(manifest, project, path, record_changes=None, member_changes=None, runtime=None):
    binaries = {name + ".exe": ("MZ\x00Synthetic " + name + " Windows executable\r\n").encode()
                for name in project["binaries"]}
    runtime = RUNTIME if runtime is None else runtime
    lockfile = b'version = 4\n\n[[package]]\nname = "fixture"\nversion = "1.0.0"\n'
    record = copy.deepcopy(project) | {
        "target": manifest["target"], "pipeline_commit": manifest["pipeline_commit"],
        "release_tag": manifest["release_tag"], "patches": [],
        "package_version": "0.1.0" if project["id"] == "wot" else project["version"],
        "rustc": "rustc 1.90.0 (fixture)\nhost: x86_64-pc-windows-msvc",
        "cargo": "cargo 1.90.0 (fixture)",
        "upstream_lockfile_sha256": digest(lockfile), "build_lockfile_sha256": digest(lockfile),
        "binaries_sha256": {name: digest(data) for name, data in binaries.items()},
        "runtime_sha256": {name: digest(data) for name, data in runtime.items()},
    }
    record.update(record_changes or {})
    notices = "share/" + project["id"] + "/"
    members = {"bin/" + name: data for name, data in (binaries | runtime).items()} | {
        notices + "build.json": json.dumps(record).encode(),
        notices + "LICENSE.txt": b"Synthetic upstream license fixture\n",
        notices + "Cargo.lock": lockfile,
        notices + "Cargo.upstream.lock": lockfile,
        notices + "upstream-source.tar.gz": source_archive(project),
        notices + "dependencies.json": b'[{"name":"fixture","version":"1.0.0","license":"MIT"}]\n',
        notices + "assets/completion.ps1": b"# Synthetic PowerShell completion\n",
    }
    members.update(member_changes or {})
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)


def write_components(manifest, artifacts):
    for project in manifest["projects"]:
        write_component(manifest, project, artifacts / project["id"] / component_name(project))


def github_release(manifest, *, draft=False, prerelease=False):
    return {"id": 12345, "tag_name": manifest["release_tag"], "draft": draft,
            "prerelease": prerelease, "html_url": "https://github.com/example/sequoia-windows/releases/12345",
            "assets": [{"id": index, "name": name, "state": "uploaded", "size": 2048}
                       for index, name in enumerate(sorted(download_names(manifest)), 1)]}


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.manifest = sources_manifest()
        self.artifacts = self.directory / "artifacts"
        self.staging = self.directory / "complete"
        self.output = self.directory / "downloads"
        write_components(self.manifest, self.artifacts)

    def archive_path(self, project):
        return self.artifacts / project["id"] / component_name(project)

    def collect(self):
        return packager.collect(self.manifest, self.artifacts, self.staging, self.output)

    def assert_no_complete_download(self):
        self.assertFalse((self.output / "sequoia-all-windows-x86_64.zip").exists())
        self.assertFalse((self.output / "sources.json").exists())
        self.assertFalse((self.output / "SHA256SUMS").exists())

    def test_all_components_merge_identical_runtime_and_retain_sources_and_provenance(self):
        report = self.collect()
        packager.finish(report, self.staging, self.output)
        expected_binaries = {name + ".exe" for project in self.manifest["projects"]
                             for name in project["binaries"]}
        self.assertEqual({path.name for path in (self.staging / "bin").iterdir()},
                         expected_binaries | set(RUNTIME))
        for filename, data in RUNTIME.items():
            self.assertEqual((self.staging / "bin" / filename).read_bytes(), data)
        self.assertEqual({record["id"] for record in report["builds"]},
                         {project["id"] for project in self.manifest["projects"]})
        self.assertEqual({path.name for path in self.output.iterdir()}, download_names(self.manifest))
        with zipfile.ZipFile(self.output / "sequoia-all-windows-x86_64.zip") as archive:
            bundled = json.loads(archive.read("sources.json"))
            self.assertEqual(bundled, json.loads((self.output / "sources.json").read_text()))
            self.assertEqual(bundled["pipeline_commit"], PIPELINE_COMMIT)
            for project in self.manifest["projects"]:
                notices = "share/" + project["id"] + "/"
                self.assertIn(notices + "LICENSE.txt", archive.namelist())
                self.assertIn(notices + "Cargo.upstream.lock", archive.namelist())
                with tarfile.open(fileobj=io.BytesIO(archive.read(notices + "upstream-source.tar.gz"))) as source:
                    self.assertIn(project["repository"].encode(), source.extractfile("README.md").read())

    def test_missing_duplicate_or_unexpected_component_archives_refuse_assembly(self):
        for change in ("missing", "duplicate", "unexpected"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                artifacts = directory / "artifacts"
                write_components(self.manifest, artifacts)
                project = self.manifest["projects"][0]
                original = artifacts / project["id"] / component_name(project)
                if change == "missing":
                    original.unlink()
                else:
                    destination = artifacts / "extra" / (original.name if change == "duplicate" else "unrelated.zip")
                    destination.parent.mkdir()
                    shutil.copy2(original, destination)
                with self.assertRaisesRegex(ValueError, "partial release"):
                    packager.collect(self.manifest, artifacts, directory / "complete", directory / "downloads")
                self.assertFalse((directory / "downloads" / "sequoia-all-windows-x86_64.zip").exists())

    def test_wrong_source_commit_refuses_assembly(self):
        project = self.manifest["projects"][0]
        write_component(self.manifest, project, self.archive_path(project), record_changes={"commit": "f" * 40})
        with self.assertRaisesRegex(ValueError, "Artifact source mismatch.*commit"):
            self.collect()
        self.assert_no_complete_download()

    def test_wrong_target_or_pipeline_provenance_refuses_assembly(self):
        for changes in ({"target": "x86_64-unknown-linux-gnu"},
                        {"pipeline_commit": "f" * 40}, {"release_tag": "windows-ffffffffffffffff"}):
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                artifacts = directory / "artifacts"
                write_components(self.manifest, artifacts)
                project = self.manifest["projects"][0]
                write_component(self.manifest, project, artifacts / project["id"] / component_name(project),
                                record_changes=changes)
                with self.assertRaisesRegex(ValueError, "Artifact build mismatch"):
                    packager.collect(self.manifest, artifacts, directory / "complete", directory / "downloads")
                self.assertFalse((directory / "downloads" / "sources.json").exists())

    def test_tampered_executable_fails_its_recorded_checksum(self):
        project = self.manifest["projects"][0]
        filename = next(iter(project["binaries"])) + ".exe"
        write_component(self.manifest, project, self.archive_path(project),
                        member_changes={"bin/" + filename: b"MZ\x00tampered executable"})
        with self.assertRaisesRegex(ValueError, "Binary checksum mismatch"):
            self.collect()
        self.assert_no_complete_download()

    def test_different_runtime_dlls_cannot_be_silently_overwritten(self):
        project = self.manifest["projects"][0]
        changed_runtime = RUNTIME | {"vcruntime140.dll": b"MZ\x00a different runtime build"}
        write_component(self.manifest, project, self.archive_path(project), runtime=changed_runtime)
        with self.assertRaisesRegex(ValueError, "Conflicting packaged files"):
            self.collect()
        self.assert_no_complete_download()

    def test_unconfigured_executable_cannot_enter_the_complete_bundle(self):
        project = self.manifest["projects"][0]
        write_component(self.manifest, project, self.archive_path(project),
                        member_changes={"bin/gpg-agent.exe": b"MZ\x00an unrequested external utility"})
        with self.assertRaisesRegex(ValueError, "exactly the expected utilities"):
            self.collect()
        self.assert_no_complete_download()

    def test_zip_traversal_and_windows_absolute_paths_are_rejected(self):
        unsafe_names = ("../outside.txt", "bin/../../outside.txt", "/outside.txt",
                        "C:/outside.txt", "bin\\..\\outside.txt")
        for name in unsafe_names:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                archive_path = directory / "malicious.zip"
                with zipfile.ZipFile(archive_path, "w") as archive:
                    archive.writestr(name, b"should never be extracted")
                with zipfile.ZipFile(archive_path) as archive, self.assertRaisesRegex(ValueError, "Unsafe archive member"):
                    packager.extract_checked(archive, directory / "staging")
                self.assertFalse((directory / "outside.txt").exists())


class ReleaseStatusTests(unittest.TestCase):
    def setUp(self):
        self.manifest = sources_manifest()

    def test_published_release_needs_every_nonempty_uploaded_download(self):
        release = github_release(self.manifest)
        self.assertTrue(release_status.complete_release(release, self.manifest))
        for invalid in (None, github_release(self.manifest, draft=True),
                        github_release(self.manifest, prerelease=True),
                        release | {"assets": release["assets"][:-1]}):
            with self.subTest(release=invalid):
                self.assertFalse(release_status.complete_release(invalid, self.manifest))
        for changes in ({"state": "starter"}, {"size": 0}):
            incomplete = copy.deepcopy(release)
            incomplete["assets"][0].update(changes)
            with self.subTest(changes=changes):
                self.assertFalse(release_status.complete_release(incomplete, self.manifest))

    def test_github_release_api_returns_release_or_not_found_and_propagates_auth_errors(self):
        release = github_release(self.manifest)
        endpoint = "https://api.github.com/repos/example/sequoia-windows/releases/tags/windows-test%2Fref"
        for response in (release, 404, 403):
            with self.subTest(response=response):
                if isinstance(response, int):
                    error = urllib.error.HTTPError(endpoint, response, "API fixture", {}, None)
                    patch = mock.patch.object(release_status.urllib.request, "urlopen", side_effect=error)
                else:
                    patch = mock.patch.object(release_status.urllib.request, "urlopen",
                                              return_value=io.BytesIO(json.dumps(response).encode()))
                with patch as request, mock.patch.dict(os.environ, {"GH_TOKEN": "fixture-token"}):
                    if response == 403:
                        with self.assertRaises(urllib.error.HTTPError):
                            release_status.find_release("example/sequoia-windows", "windows-test/ref")
                    else:
                        result = release_status.find_release("example/sequoia-windows", "windows-test/ref")
                        self.assertEqual(result, None if response == 404 else release)
                self.assertEqual(request.call_args.args[0].full_url, endpoint)

    def test_only_scheduled_complete_releases_skip_a_build(self):
        cases = [("schedule", github_release(self.manifest), "false"),
                 ("schedule", github_release(self.manifest, draft=True), "true"),
                 ("schedule", None, "true"),
                 ("workflow_dispatch", github_release(self.manifest), "true")]
        for event, release, expected in cases:
            with self.subTest(event=event, release=release), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                sources = directory / "sources.json"
                sources.write_text(json.dumps(self.manifest))
                output = directory / "github-output.txt"
                with mock.patch("sys.argv", ["release_status.py", "--sources", str(sources),
                                             "--github-output", str(output)]), \
                        mock.patch.dict(os.environ, {"GITHUB_EVENT_NAME": event,
                                                     "GITHUB_REPOSITORY": "example/sequoia-windows"}), \
                        mock.patch.object(release_status, "find_release", return_value=release) as lookup, \
                        contextlib.redirect_stdout(io.StringIO()):
                    release_status.main()
                self.assertEqual(output.read_text(), "should_build=" + expected + "\n")
                self.assertEqual(lookup.call_count, 1 if event == "schedule" else 0)


class PublishTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.manifest = sources_manifest()
        artifacts = self.directory / "artifacts"
        staging = self.directory / "complete"
        self.downloads = self.directory / "downloads"
        write_components(self.manifest, artifacts)
        report = packager.collect(self.manifest, artifacts, staging, self.downloads)
        packager.finish(report, staging, self.downloads)

    def publish(self):
        with mock.patch.dict(os.environ, {"GH_REPO": "example/sequoia-windows"}), \
                contextlib.redirect_stdout(io.StringIO()):
            publisher.publish(self.downloads)

    def test_tampered_download_is_rejected_before_network_lookup_or_gh_mutation(self):
        archive = self.downloads / "sequoia-all-windows-x86_64.zip"
        archive.write_bytes(archive.read_bytes() + b"tampered after packaging")
        with mock.patch.object(publisher, "find_release") as lookup, mock.patch.object(publisher, "run") as gh:
            with self.assertRaisesRegex(ValueError, "Download checksum mismatch"):
                self.publish()
        lookup.assert_not_called()
        gh.assert_not_called()

    def test_missing_download_does_not_create_a_partial_release(self):
        (self.downloads / component_name(self.manifest["projects"][0])).unlink()
        with mock.patch.object(publisher, "find_release") as lookup, mock.patch.object(publisher, "run") as gh:
            with self.assertRaisesRegex(ValueError, "incomplete or unexpected"):
                self.publish()
        lookup.assert_not_called()
        gh.assert_not_called()

    def test_unexpected_download_cannot_be_published(self):
        (self.downloads / "unrequested.exe").write_bytes(b"MZ\x00unexpected download")
        with mock.patch.object(publisher, "find_release") as lookup, mock.patch.object(publisher, "run") as gh:
            with self.assertRaisesRegex(ValueError, "incomplete or unexpected"):
                self.publish()
        lookup.assert_not_called()
        gh.assert_not_called()

    def test_missing_checksum_does_not_attempt_gh_mutation(self):
        path = self.downloads / "SHA256SUMS"
        lines = path.read_text().splitlines()
        path.write_text("\n".join(lines[1:]) + "\n")
        with mock.patch.object(publisher, "find_release") as lookup, mock.patch.object(publisher, "run") as gh:
            with self.assertRaisesRegex(ValueError, "Missing checksum entries"):
                self.publish()
        lookup.assert_not_called()
        gh.assert_not_called()

    def test_complete_published_release_is_idempotent(self):
        with mock.patch.object(publisher, "find_release", return_value=github_release(self.manifest)) as lookup, \
                mock.patch.object(publisher, "run") as gh:
            self.publish()
        lookup.assert_called_once_with("example/sequoia-windows", self.manifest["release_tag"])
        gh.assert_not_called()

    def test_incomplete_published_release_requires_review_and_is_not_overwritten(self):
        release = github_release(self.manifest)
        release["assets"].pop()
        with mock.patch.object(publisher, "find_release", return_value=release), mock.patch.object(publisher, "run") as gh:
            with self.assertRaisesRegex(RuntimeError, "needs review"):
                self.publish()
        gh.assert_not_called()

    def test_existing_draft_uploads_the_complete_set_before_publishing(self):
        with mock.patch.object(publisher, "find_release",
                               return_value=github_release(self.manifest, draft=True, prerelease=True)), \
                mock.patch.object(publisher, "run") as gh:
            self.publish()
        calls = [call.args[0] for call in gh.call_args_list]
        self.assertEqual([arguments[2] for arguments in calls], ["upload", "edit"])
        self.assertIn("--clobber", calls[0])
        self.assertEqual({path.name for path in calls[0] if isinstance(path, Path)}, download_names(self.manifest))
        self.assertIn("--draft=false", calls[1])
        self.assertIn("--prerelease=false", calls[1])

    def test_new_release_stays_draft_until_all_assets_have_uploaded(self):
        with mock.patch.object(publisher, "find_release", return_value=None), mock.patch.object(publisher, "run") as gh:
            self.publish()
        calls = [call.args[0] for call in gh.call_args_list]
        self.assertEqual([arguments[2] for arguments in calls], ["create", "upload", "edit"])
        self.assertIn("--draft", calls[0])
        self.assertEqual(calls[0][calls[0].index("--target") + 1], PIPELINE_COMMIT)
        self.assertIn("--draft=false", calls[-1])
        self.assertIn("--prerelease=false", calls[-1])

    def test_upload_failure_never_publishes_the_new_draft(self):
        failure = subprocess.CalledProcessError(1, ["gh", "release", "upload"])
        with mock.patch.object(publisher, "find_release", return_value=None), \
                mock.patch.object(publisher, "run", side_effect=[None, failure]) as gh:
            with self.assertRaises(subprocess.CalledProcessError):
                self.publish()
        self.assertEqual([call.args[0][2] for call in gh.call_args_list], ["create", "upload"])


if __name__ == "__main__":
    unittest.main()
