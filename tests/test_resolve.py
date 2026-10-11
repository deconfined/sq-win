"""Exercise release resolution against GitLab API fixtures, without network access."""

import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import urllib.error


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("release_resolver", ROOT / "scripts/resolve.py")
resolver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(resolver)
PROJECTS = {project["id"]: project for project in json.loads((ROOT / "projects.json").read_text())}

COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
PIPELINE_COMMIT = "c" * 40


def tag(name, commit=COMMIT_A, created_at="2026-07-03T12:00:00.000+00:00"):
    # GitLab returns both tag metadata and the peeled commit for annotated tags.
    return {
        "name": name,
        "message": "Release " + name,
        "target": commit,
        "commit": {"id": commit, "title": "Release " + name},
        "release": None,
        "protected": False,
        "created_at": created_at,
    }


def tags_url(repository, page=1):
    return (
        "https://gitlab.com/api/v4/projects/sequoia-pgp%2F"
        + repository + "/repository/tags?per_page=100&page=" + str(page)
    )


class GitLabFixture:
    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    def fetch(self, url):
        self.requests.append(url)
        if url not in self.responses:
            raise AssertionError("Unexpected GitLab request: " + url)
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)

    def urlopen(self, request, timeout):
        body, headers = self.fetch(request.full_url)
        response = io.BytesIO(json.dumps(body).encode("utf-8"))
        response.headers = headers
        return response


class StableReleaseTests(unittest.TestCase):
    def select(self, project_id, tags):
        return resolver.select_stable(tags, PROJECTS[project_id]["tag_patterns"])

    def test_latest_uses_numeric_version_not_creation_date_or_api_order(self):
        release = self.select("sqv", [
            tag("v1.4.0", created_at="2026-10-10T12:00:00.000+00:00"),
            tag("v1.5.0", created_at="2026-10-09T12:00:00.000+00:00"),
            tag("v1.10.0", COMMIT_B, "2026-01-01T12:00:00.000+00:00"),
            tag("v1.9.9"),
        ])
        self.assertEqual(release, {"version": "1.10.0", "tag": "v1.10.0", "commit": COMMIT_B})

    def test_prereleases_and_noncanonical_versions_are_excluded(self):
        release = self.select("sop", [
            tag("v0.38.0-pqc.1"), tag("v0.39.0-rc.1"), tag("v1.0.0-alpha"),
            tag("v0.39.0+build.1"), tag("v00.39.0"), tag("v0.039.0"),
            tag("v0.38.0", COMMIT_B),
        ])
        self.assertEqual(release["tag"], "v0.38.0")
        self.assertEqual(release["commit"], COMMIT_B)

    def test_sq_workspace_tags_select_sq_not_sqz(self):
        release = self.select("sq", [
            tag("sqz/v99.0.0"), tag("v1.4.0"),
            tag("sequoia-sq/v1.5.0", COMMIT_B), tag("sequoia-sq/v1.6.0-pqc.1"),
        ])
        self.assertEqual(release["tag"], "sequoia-sq/v1.5.0")

    def test_keystore_selects_server_release_not_library_or_backend_release(self):
        release = self.select("keystore", [
            tag("sequoia-keystore/v0.10.0"), tag("v99.0.0"),
            tag("softkeys/v0.9.0"), tag("server/v0.2.0"),
            tag("server/v0.3.0", COMMIT_B), tag("server/v0.4.0-rc.1"),
        ])
        self.assertEqual(release, {"version": "0.3.0", "tag": "server/v0.3.0", "commit": COMMIT_B})

    def test_cryptoki_selects_stable_cli_release_not_a_library_tag(self):
        release = self.select("cryptoki", [
            tag("sequoia-cryptoki/v99.0.0"), tag("v99.0.0"),
            tag("sq-cryptoki/v0.1.0"), tag("sq-cryptoki/v0.2.0", COMMIT_B),
            tag("sq-cryptoki/v0.3.0-pqc.1"),
        ])
        self.assertEqual(release, {"version": "0.2.0", "tag": "sq-cryptoki/v0.2.0", "commit": COMMIT_B})

    def test_wot_considers_plain_and_component_release_tags(self):
        release = self.select("wot", [
            tag("v0.15.0"), tag("sequoia-wot/v0.15.2", COMMIT_B),
            tag("sequoia-wot/v0.16.0-pqc.1"),
        ])
        self.assertEqual(release["version"], "0.15.2")
        self.assertEqual(release["tag"], "sequoia-wot/v0.15.2")

    def test_conflicting_same_version_component_tags_fail(self):
        with self.assertRaisesRegex(ValueError, "Conflicting tags"):
            self.select("sq", [tag("v1.5.0"), tag("sequoia-sq/v1.5.0", COMMIT_B)])

    def test_same_version_aliases_are_allowed_when_the_commit_is_identical(self):
        release = self.select("sq", [tag("v1.5.0"), tag("sequoia-sq/v1.5.0")])
        self.assertEqual(release["version"], "1.5.0")
        self.assertEqual(release["commit"], COMMIT_A)

    def test_missing_stable_release_or_invalid_commit_fails(self):
        for tags in ([], [tag("v1.5.0-rc.1")], [tag("v1.5.0", "abc1234")]):
            with self.subTest(tags=tags), self.assertRaises(ValueError):
                self.select("sqv", tags)


class PaginationTests(unittest.TestCase):
    def test_follows_case_insensitive_gitlab_next_page_header(self):
        first = tags_url("sequoia-sqv")
        second = tags_url("sequoia-sqv", 2)
        fixture = GitLabFixture({
            first: ([tag("v1.4.0")], {"X-Next-Page": "2"}),
            second: ([tag("v1.5.0", COMMIT_B)], {"X-Next-Page": ""}),
        })
        release = resolver.resolve_project(PROJECTS["sqv"], fixture.fetch)
        self.assertEqual(fixture.requests, [first, second])
        self.assertEqual(release["commit"], COMMIT_B)
        self.assertEqual(release["source_url"],
                         "https://gitlab.com/sequoia-pgp/sequoia-sqv/-/tree/" + COMMIT_B)

    def test_full_page_without_proxy_headers_fetches_the_next_page(self):
        first = tags_url("sequoia-sop")
        second = tags_url("sequoia-sop", 2)
        fixture = GitLabFixture({
            first: ([tag("v0.37.3")] + [tag(f"v0.38.0-rc.{n}") for n in range(99)], {}),
            second: ([tag("v0.38.0", COMMIT_B)], {}),
        })
        release = resolver.resolve_project(PROJECTS["sop"], fixture.fetch)
        self.assertEqual(fixture.requests, [first, second])
        self.assertEqual(release["tag"], "v0.38.0")

    def test_explicit_last_page_header_ends_even_a_full_page(self):
        url = tags_url("sequoia-sqv")
        fixture = GitLabFixture({url: ([tag("v1.5.0")] * 100, {"X-Next-Page": ""})})
        resolver.resolve_project(PROJECTS["sqv"], fixture.fetch)
        self.assertEqual(fixture.requests, [url])

    def test_invalid_or_repeated_next_page_fails(self):
        for next_page in ("not-a-page", "1"):
            with self.subTest(next_page=next_page):
                fixture = GitLabFixture({
                    tags_url("sequoia-sqv"): ([tag("v1.5.0")], {"X-Next-Page": next_page}),
                })
                with self.assertRaisesRegex(ValueError, "page|pagination"):
                    resolver.resolve_project(PROJECTS["sqv"], fixture.fetch)

    def test_api_error_object_is_not_treated_as_a_tag_list(self):
        fixture = GitLabFixture({tags_url("sequoia-sqv"): ({"message": "404 Project Not Found"}, {})})
        with self.assertRaisesRegex(ValueError, "not a list"):
            resolver.resolve_project(PROJECTS["sqv"], fixture.fetch)


class ManifestTests(unittest.TestCase):
    def project(self):
        fixture = GitLabFixture({tags_url("sequoia-sqv"): ([tag("v1.5.0")], {})})
        return resolver.resolve_project(PROJECTS["sqv"], fixture.fetch)

    def test_release_fingerprint_is_stable_and_records_pipeline_and_sources(self):
        project = self.project()
        manifest = resolver.make_manifest([project], PIPELINE_COMMIT)
        reordered = dict(reversed(list(project.items())))
        self.assertEqual(manifest, resolver.make_manifest([reordered], PIPELINE_COMMIT))
        self.assertEqual(manifest["pipeline_commit"], PIPELINE_COMMIT)
        self.assertEqual(manifest["projects"][0]["commit"], COMMIT_A)
        self.assertEqual(manifest["target"], "x86_64-pc-windows-msvc")
        self.assertEqual(manifest["schema"], 1)
        self.assertRegex(manifest["release_tag"], r"^windows-[0-9a-f]{16}$")

    def test_pipeline_commit_upstream_commit_and_build_configuration_change_identity(self):
        project = self.project()
        original = resolver.make_manifest([project], PIPELINE_COMMIT)["release_tag"]
        variants = [
            ([project], "d" * 40),
            ([project | {"commit": COMMIT_B, "source_url": project["source_url"].replace(COMMIT_A, COMMIT_B)}], PIPELINE_COMMIT),
            ([project | {"features": ["crypto-openssl"]}], PIPELINE_COMMIT),
        ]
        for projects, pipeline_commit in variants:
            with self.subTest(projects=projects, pipeline_commit=pipeline_commit):
                self.assertNotEqual(original, resolver.make_manifest(projects, pipeline_commit)["release_tag"])

    def test_pipeline_commit_must_be_a_full_sha(self):
        for commit in ("abc1234", "", "C" * 40, "g" * 40):
            with self.subTest(commit=commit), self.assertRaises(ValueError):
                resolver.make_manifest([self.project()], commit)


class CommandTests(unittest.TestCase):
    def run_command(self, directory, fixture):
        output = directory / "sources.json"
        github_output = directory / "github-output.txt"
        args = ["resolve.py", "--pipeline-commit", PIPELINE_COMMIT,
                "--output", str(output), "--github-output", str(github_output)]
        with mock.patch.object(resolver, "ROOT", directory), \
                mock.patch("sys.argv", args), \
                mock.patch.object(resolver.urllib.request, "urlopen", fixture.urlopen), \
                contextlib.redirect_stdout(io.StringIO()):
            resolver.main()
        return output, github_output

    def config(self, directory):
        (directory / "projects.json").write_text(json.dumps([PROJECTS["sqv"], PROJECTS["sop"]]))

    def test_manifest_and_github_outputs_share_release_identity_and_complete_matrix(self):
        fixture = GitLabFixture({
            tags_url("sequoia-sqv"): ([tag("v1.5.0")], {}),
            tags_url("sequoia-sop"): ([tag("v0.38.0", COMMIT_B)], {}),
        })
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.config(directory)
            output, github_output = self.run_command(directory, fixture)
            manifest = json.loads(output.read_text())
            github = dict(line.split("=", 1) for line in github_output.read_text().splitlines())
        self.assertEqual([project["id"] for project in manifest["projects"]], ["sqv", "sop"])
        self.assertEqual(github["release_tag"], manifest["release_tag"])
        self.assertEqual(json.loads(github["matrix"]), {"include": [{"project": "sqv"}, {"project": "sop"}]})
        self.assertEqual(manifest["pipeline_commit"], PIPELINE_COMMIT)

    def test_empty_or_failed_project_does_not_emit_a_partial_release(self):
        failures = [
            ([], {}),
            urllib.error.HTTPError(tags_url("sequoia-sop"), 403, "Forbidden", {}, None),
        ]
        for failure in failures:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                self.config(directory)
                fixture = GitLabFixture({
                    tags_url("sequoia-sqv"): ([tag("v1.5.0")], {}),
                    tags_url("sequoia-sop"): failure,
                })
                with self.assertRaises((ValueError, urllib.error.HTTPError)):
                    self.run_command(directory, fixture)
                self.assertFalse((directory / "sources.json").exists())
                self.assertFalse((directory / "github-output.txt").exists())


if __name__ == "__main__":
    unittest.main()
