"""The three images under test, each built once from its reviewed context.

`python -m tests.support.images` builds them, as the CI `images` job does;
tests take them from the session fixtures in `tests/support/fixtures.py`.
When `HERMES_TEST_IMAGE_ARCHIVES` holds a saved build of an image (CI's
`<tag>.tar.zst`), that archive is the image under test and nothing is rebuilt.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agent_platform.build import image_build
from agent_platform.privacy import ROOT, stage_contexts

HERMES_IMAGE = "personal-hermes-native-test:local"
MANAGER_IMAGE = "hermes-manager:e2e"
WORKER_TAG = "hermes-next-worker:test"
TAGS = {"hermes": HERMES_IMAGE, "manager": MANAGER_IMAGE, "worker": WORKER_TAG}


class ImageUnavailable(RuntimeError):
    """An image under test is neither built nor saved; never a skip."""


def archive(tag):
    """CI's saved build of `tag`, when there is one."""
    directory = os.environ.get("HERMES_TEST_IMAGE_ARCHIVES")
    if directory:
        path = Path(directory) / (tag.replace("/", "_").replace(":", "_") + ".tar.zst")
        if path.exists():
            return path
    return None


def image_source(tag):
    """The command that writes the image's archive: CI's saved build, or Docker."""
    path = archive(tag)
    if path:
        return ["zstd", "-dc", str(path)]
    return ["docker", "save", tag]


def available(tag):
    if archive(tag):
        return True
    inspected = subprocess.run(
        ["docker", "image", "inspect", tag], capture_output=True, timeout=60
    )
    return inspected.returncode == 0


def _build(name, command):
    if subprocess.run(command, cwd=ROOT, timeout=3600).returncode:
        raise ImageUnavailable(f"building the {name} image failed")


def build(name, *, github_cache=False):
    """Builds one image under test from its reviewed release context."""
    if name == "manager":
        command = [sys.executable, "-m", "agent_platform.manager_build"]
        command += ["--tag", MANAGER_IMAGE]
        _build(name, [*command, *(["--github-cache"] if github_cache else [])])
        return
    target = {"hermes": "hermes", "worker": "agent-runtime"}[name]
    with tempfile.TemporaryDirectory(prefix=f"{name}-build-") as directory:
        context = stage_contexts(ROOT, Path(directory) / "inputs")[target]
        command = [*image_build(target, github_cache=github_cache)]
        if name == "hermes":
            command += ["--file", str(context / "runtimes/hermes/Dockerfile")]
        _build(name, [*command, "--tag", TAGS[name], str(context)])


def build_all(names, *, github_cache=False):
    """Builds the named images side by side."""
    with ThreadPoolExecutor(max_workers=max(len(names), 1)) as pool:
        list(pool.map(lambda name: build(name, github_cache=github_cache), names))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("names", nargs="*", help=f"some of {', '.join(TAGS)}")
    parser.add_argument("--github-cache", action="store_true")
    args = parser.parse_args()
    unknown = set(args.names) - set(TAGS)
    if unknown:
        parser.error(f"unknown images: {', '.join(sorted(unknown))}")
    build_all(args.names or list(TAGS), github_cache=args.github_cache)


if __name__ == "__main__":
    main()
