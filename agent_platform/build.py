"""Share image targets, labels and GitHub Actions cache settings across builds."""

from __future__ import annotations

import os
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "https://github.com/Accelero/agent-platform-v2"
TARGETS = (
    "hermes",
    "agent-runtime",
    "hermes-manager",
)


def _context_arguments(contexts: dict[str, Path]) -> list[str]:
    args = []
    for name, path in contexts.items():
        args.extend(["--set", name + ".context=" + str(path)])
    return args


def _github_cache(name: str) -> str:
    """One scope per image; the runner's credentials stay in its environment."""
    if os.environ.get("GITHUB_ACTIONS") != "true" or not all(
        os.environ.get(name)
        for name in ("ACTIONS_RUNTIME_TOKEN", "ACTIONS_RESULTS_URL")
    ):
        raise ValueError("GitHub Actions cache requires the exported runner runtime.")
    return f"type=gha,version=2,scope=agent-platform-{name}-amd64"


def _fills_github_cache() -> bool:
    """Only a push to main stores layers: every other run can only read them."""
    return (
        os.environ.get("GITHUB_EVENT_NAME") == "push"
        and os.environ.get("GITHUB_REF") == "refs/heads/main"
    )


def image_labels(root: Path = ROOT) -> list[str]:
    """The source, revision and version labels of every image build.

    A release publishes the very images its tests ran, so they carry the
    labels from the start: the version is the one the source declares, the
    tag's version on a release commit and the placeholder otherwise.
    """
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        check=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    labels = {
        "org.opencontainers.image.source": REPOSITORY,
        "org.opencontainers.image.revision": revision,
        "org.opencontainers.image.version": version,
    }
    return [part for item in labels.items() for part in ("--label", "=".join(item))]


def bake_arguments(*, contexts: dict[str, Path] | None = None) -> list[str]:
    """The local release build, which never reads or writes the CI cache."""
    args = [
        "docker",
        "buildx",
        "bake",
        "--file",
        "containers/docker-bake.hcl",
        *TARGETS,
        "--progress",
        "plain",
        "--provenance=mode=min",
        "--set",
        "*.platform=linux/amd64",
    ]
    args.extend(_context_arguments(contexts or {}))
    return args


def image_build(name: str, *, github_cache: bool = False) -> list[str]:
    """The build of an image under test; in CI it reuses the cache main fills."""
    if not github_cache:
        return ["docker", "build", *image_labels()]
    cache = _github_cache(name)
    args = ["docker", "buildx", "build", "--load", "--cache-from", cache]
    if _fills_github_cache():
        # A cache problem costs speed; it must never fail the build.
        args.extend(["--cache-to", f"{cache},mode=max,ignore-error=true"])
    return [*args, *image_labels()]
