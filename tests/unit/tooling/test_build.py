"""Protect cache isolation, credential handling and release publication boundaries."""

import tomllib
from pathlib import Path

import pytest
import yaml

from agent_platform import build, release

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def github_runtime(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("ACTIONS_RUNTIME_TOKEN", "fixture-runtime-credential")
    monkeypatch.setenv("ACTIONS_RESULTS_URL", "https://fixture.invalid/cache")


def test_github_cache_scopes_are_independent_and_credentials_stay_in_environment(
    github_runtime,
):
    args = build.bake_arguments(github_cache=True)
    imports = [value for value in args if ".cache-from=" in value]
    assert len(imports) == len(build.TARGETS)
    assert not any(".cache-to=" in value for value in args)
    assert len({value.split("scope=")[1] for value in imports}) == len(build.TARGETS)
    assert all("type=gha,version=2" in value for value in imports)
    assert not any("fixture-runtime-credential" in value for value in args)
    assert not any("fixture.invalid" in value for value in args)
    assert not any(
        "type=registry" in value or "-cache:build" in value for value in args
    )


def test_local_builds_do_not_require_runner_credentials(monkeypatch):
    monkeypatch.delenv("ACTIONS_RUNTIME_TOKEN", raising=False)
    args = build.bake_arguments()
    assert not any("cache-from=" in value or "cache-to=" in value for value in args)


def test_missing_cache_runtime_stops_before_external_operations(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("ACTIONS_RUNTIME_TOKEN", raising=False)
    with pytest.raises(ValueError, match="runner runtime"):
        build.bake_arguments(github_cache=True)
    with pytest.raises(ValueError, match="runner runtime"):
        build.image_build("hermes", github_cache=True)


def test_the_job_that_fills_the_cache_cannot_publish_packages():
    workflow = yaml.safe_load((build.ROOT / ".github/workflows/checks.yml").read_text())
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["images"]
    assert job["if"] == "github.event_name != 'pull_request'"
    commands = "\n".join(step.get("run", "") for step in job["steps"])
    assert "tests.run build --github-cache" in commands
    assert "--publish" not in commands
    assert "--push" not in commands
    assert not any("login-action" in step.get("uses", "") for step in job["steps"])


def test_release_workflow_uses_the_shared_cache_configuration():
    workflow = yaml.safe_load(
        (build.ROOT / ".github/workflows/release.yml").read_text()
    )
    commands = "\n".join(
        step.get("run", "") for step in workflow["jobs"]["publish"]["steps"]
    )
    assert "--publish --github-cache" in commands
    assert release.TARGETS == build.TARGETS


@pytest.mark.parametrize(
    ("pattern", "version"),
    [
        ("runtimes/hermes/*.py", (3, 13)),
        ("runtimes/hermes/plugins/managed_execution/*.py", (3, 13)),
    ],
)
def test_image_code_parses_on_its_image_python(pattern, version):
    """Code runs on each image's interpreter, not the 3.14 used for development."""
    import ast

    files = sorted(build.ROOT.glob(pattern))
    assert files
    for path in files:
        ast.parse(path.read_text(), filename=str(path), feature_version=version)


def test_release_images_never_build_fault_injection():
    """The manager image builds its binaries without features, and the
    process-aborting test feature is not a default."""
    manager = ROOT / "services/hermes-manager"
    dockerfile = (manager / "Dockerfile").read_text()
    assert "cargo build --release --locked" in dockerfile
    assert "fault-injection" not in dockerfile
    assert "--features" not in dockerfile and "--all-features" not in dockerfile
    features = tomllib.loads(
        (manager / "crates/hermes-manager/Cargo.toml").read_text()
    )["features"]
    assert features["fault-injection"] == []
    assert "fault-injection" not in features.get("default", [])


def test_only_a_push_to_main_writes_the_image_cache(github_runtime, monkeypatch):
    assert build.image_build("hermes") == ["docker", "build"]
    for event, ref in (
        ("push", "refs/tags/v1.2.3"),
        ("push", "refs/heads/feature"),
        ("pull_request", "refs/heads/main"),
    ):
        monkeypatch.setenv("GITHUB_EVENT_NAME", event)
        monkeypatch.setenv("GITHUB_REF", ref)
        cached = build.image_build("hermes-manager", github_cache=True)
        assert "--cache-from" in cached and "--load" in cached
        assert "scope=agent-platform-hermes-manager-amd64" in " ".join(cached)
        assert "--cache-to" not in cached
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    cached = build.image_build("hermes-manager", github_cache=True)
    written = cached[cached.index("--cache-to") + 1]
    assert "scope=agent-platform-hermes-manager-amd64" in written
    assert "ignore-error=true" in written
    assert "--push" not in cached
