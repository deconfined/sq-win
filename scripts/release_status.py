#!/usr/bin/env python3
"""Skip scheduled builds only when their complete release is already published."""

import argparse
import json
import os
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

from build_project import ROOT


def find_release(repository, tag):
    url = f"https://api.github.com/repos/{repository}/releases/tags/{urllib.parse.quote(tag, safe='')}"
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json", "User-Agent": "sequoia-windows-builds",
        "Authorization": "Bearer " + os.environ["GH_TOKEN"],
    })
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def expected_assets(manifest):
    return {f"{p['repository']}-{p['version']}-windows-x86_64.zip" for p in manifest["projects"]} | {
        "sequoia-all-windows-x86_64.zip", "sources.json", "SHA256SUMS", "release-notes.md"}


def complete_release(release, manifest):
    return bool(release and not release["draft"] and not release["prerelease"]
                and expected_assets(manifest) <= {
                    asset["name"] for asset in release["assets"]
                    if asset["state"] == "uploaded" and asset["size"] > 0})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, default=ROOT / "sources.json")
    parser.add_argument("--github-output", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.sources.read_text(encoding="utf-8"))
    should_build = True
    if os.environ.get("GITHUB_EVENT_NAME") == "schedule":
        should_build = not complete_release(find_release(os.environ["GITHUB_REPOSITORY"],
                                                         manifest["release_tag"]), manifest)
    with args.github_output.open("a", encoding="utf-8") as output:
        output.write("should_build=" + str(should_build).lower() + "\n")
    print("Build required: " + str(should_build), flush=True)


if __name__ == "__main__":
    main()
