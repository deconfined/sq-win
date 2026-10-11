#!/usr/bin/env python3
"""Publish a complete, checksum-verified collection as one GitHub Release."""

import argparse
import json
import os
from pathlib import Path

from build_project import run, sha256
from release_status import complete_release, expected_assets, find_release


def publish(downloads):
    manifest = json.loads((downloads / "sources.json").read_text(encoding="utf-8"))
    expected = expected_assets(manifest)
    if {path.name for path in downloads.iterdir() if path.is_file()} != expected:
        raise ValueError("Refusing to publish an incomplete or unexpected set of downloads")
    lines = (downloads / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    checksums = {}
    for line in lines:
        digest, name = line.split("  ", 1)
        if name in checksums or name not in expected - {"SHA256SUMS"}:
            raise ValueError("Unexpected or duplicate checksum entry")
        checksums[name] = digest
    if set(checksums) != expected - {"SHA256SUMS"}:
        raise ValueError("Missing checksum entries")
    for name, digest in checksums.items():
        if sha256(downloads / name) != digest:
            raise ValueError("Download checksum mismatch: " + name)
    repository = os.environ["GH_REPO"]
    tag = manifest["release_tag"]
    release = find_release(repository, tag)
    if complete_release(release, manifest):
        print("This complete release is already published: " + release["html_url"], flush=True)
        return
    if release and not release["draft"]:
        raise RuntimeError("An incomplete published release needs review; it will not be overwritten")
    if not release:
        run(["gh", "release", "create", tag, "--draft", "--target", manifest["pipeline_commit"],
             "--title", "Sequoia for Windows x64 (" + tag + ")", "--notes-file", downloads / "release-notes.md"])
    run(["gh", "release", "upload", tag, "--clobber", *[downloads / name for name in sorted(expected)]])
    run(["gh", "release", "edit", tag, "--draft=false", "--latest", "--notes-file", downloads / "release-notes.md"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--downloads", type=Path, required=True)
    args = parser.parse_args()
    publish(args.downloads.resolve())


if __name__ == "__main__":
    main()
