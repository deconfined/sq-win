#!/usr/bin/env python3
"""Resolve stable component releases, including component-specific workspace tags."""

import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
TARGET = "x86_64-pc-windows-msvc"


def request_json(url):
    for attempt in range(4):
        request = urllib.request.Request(url, headers={"User-Agent": "sequoia-windows-builds"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response), dict(response.headers)
        except urllib.error.HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504) or attempt == 3:
                raise
            time.sleep(min(int(error.headers.get("Retry-After", "5")), 60))
        except urllib.error.URLError:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("Request retry limit exceeded")


def select_stable(tags, patterns):
    candidates = []
    for tag in tags:
        for pattern in patterns:
            match = re.fullmatch(pattern, tag["name"])
            if match:
                version = tuple(int(part) for part in match.groups())
                # SemVer does not permit leading zeroes in numeric components.
                if any(str(number) != text for number, text in zip(version, match.groups())):
                    break
                sha = tag["commit"]["id"]
                if not re.fullmatch(r"[0-9a-f]{40}", sha):
                    raise ValueError(f"Invalid commit for {tag['name']}: {sha}")
                candidates.append((version, tag["name"], sha))
                break
    if not candidates:
        raise ValueError("No stable release tags matched the configured component patterns")
    version = max(candidate[0] for candidate in candidates)
    latest = [candidate for candidate in candidates if candidate[0] == version]
    if len({candidate[2] for candidate in latest}) != 1:
        raise ValueError(f"Conflicting tags for stable version {version}: {latest}")
    _, name, sha = sorted(latest)[-1]
    return {"version": ".".join(map(str, version)), "tag": name, "commit": sha}


def resolve_project(project, fetch=request_json):
    encoded = urllib.parse.quote("sequoia-pgp/" + project["repository"], safe="")
    base = f"https://gitlab.com/api/v4/projects/{encoded}/repository/tags"
    tags = []
    page = "1"
    seen = set()
    while page:
        if page in seen or len(seen) >= 100:
            raise ValueError("Invalid or excessive GitLab tag pagination")
        seen.add(page)
        batch, headers = fetch(f"{base}?per_page=100&page={page}")
        if not isinstance(batch, list):
            raise ValueError("GitLab tags response was not a list")
        tags.extend(batch)
        headers = {key.lower(): value for key, value in headers.items()}
        page = headers.get("x-next-page", "")
        # Some proxies omit pagination headers; do not silently truncate tags.
        if "x-next-page" not in headers and len(batch) == 100:
            page = str(max(map(int, seen)) + 1)
        if page and not page.isdecimal():
            raise ValueError("GitLab returned a non-numeric next page")
    release = select_stable(tags, project["tag_patterns"])
    return project | release | {
        "url": f"https://gitlab.com/sequoia-pgp/{project['repository']}.git",
        "source_url": f"https://gitlab.com/sequoia-pgp/{project['repository']}/-/tree/{release['commit']}",
    }


def make_manifest(projects, pipeline_commit):
    if not re.fullmatch(r"[0-9a-f]{40}", pipeline_commit):
        raise ValueError("The pipeline commit must be a full 40-character Git SHA")
    identity = {"target": TARGET, "pipeline_commit": pipeline_commit, "projects": projects}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:16]
    return identity | {"schema": 1, "release_tag": "windows-" + digest}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "sources.json")
    parser.add_argument("--pipeline-commit", default=os.environ.get("GITHUB_SHA"))
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    if not args.pipeline_commit:
        parser.error("--pipeline-commit is required outside GitHub Actions")
    config = json.loads((ROOT / "projects.json").read_text())
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(config)) as pool:
        projects = list(pool.map(resolve_project, config))
    manifest = make_manifest(projects, args.pipeline_commit)
    args.output.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    for project in projects:
        print(f"{project['id']}: {project['tag']} ({project['commit']})", flush=True)
    if args.github_output:
        matrix = {"include": [{"project": project["id"]} for project in projects]}
        with args.github_output.open("a", encoding="utf-8") as output:
            output.write("matrix=" + json.dumps(matrix, separators=(",", ":")) + "\n")
            output.write("release_tag=" + manifest["release_tag"] + "\n")


if __name__ == "__main__":
    main()
