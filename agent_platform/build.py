"""Share image targets and GitHub Actions cache settings across CI builds."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
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


def bake_arguments(
    *, github_cache: bool = False, contexts: dict[str, Path] | None = None
) -> list[str]:
    """The release build, optionally reading the cache that main fills."""
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
    if github_cache:
        for name in TARGETS:
            args.extend(["--set", f"{name}.cache-from={_github_cache(name)}"])
    return args


def image_build(name: str, *, github_cache: bool = False) -> list[str]:
    """The build of an image under test; in CI it reuses the cache main fills."""
    if not github_cache:
        return ["docker", "build"]
    cache = _github_cache(name)
    args = ["docker", "buildx", "build", "--load", "--cache-from", cache]
    if _fills_github_cache():
        # A cache problem costs speed; it must never fail the build.
        args.extend(["--cache-to", f"{cache},mode=max,ignore-error=true"])
    return args
