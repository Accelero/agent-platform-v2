"""Protect cache isolation, credential handling and release publication boundaries."""

import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

from agent_platform import build, release

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def github_runtime(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("ACTIONS_RUNTIME_TOKEN", "fixture-runtime-credential")
    monkeypatch.setenv("ACTIONS_RESULTS_URL", "https://fixture.invalid/cache")


def test_github_cache_scopes_are_independent_and_credentials_stay_in_environment(
    github_runtime,
):
    args = [build.image_build(name, github_cache=True) for name in build.TARGETS]
    imports = [command[command.index("--cache-from") + 1] for command in args]
    assert len({value.split("scope=")[1] for value in imports}) == len(build.TARGETS)
    assert all("type=gha,version=2" in value for value in imports)
    flat = [value for command in args for value in command]
    assert not any("fixture-runtime-credential" in value for value in flat)
    assert not any("fixture.invalid" in value for value in flat)
    assert not any(
        "type=registry" in value or "-cache:build" in value for value in flat
    )


def test_local_builds_do_not_require_runner_credentials(monkeypatch):
    monkeypatch.delenv("ACTIONS_RUNTIME_TOKEN", raising=False)
    for args in (build.bake_arguments(), build.image_build("hermes")):
        assert not any("cache-from" in value or "cache-to" in value for value in args)


def test_missing_cache_runtime_stops_before_external_operations(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("ACTIONS_RUNTIME_TOKEN", raising=False)
    with pytest.raises(ValueError, match="runner runtime"):
        build.image_build("hermes", github_cache=True)


def test_every_image_build_carries_the_source_revision_and_build_version(
    monkeypatch,
):
    monkeypatch.delenv("GITHUB_REF_TYPE", raising=False)
    labels = build.image_labels()
    pairs = dict(labels[index + 1].split("=", 1) for index in range(0, len(labels), 2))
    assert set(labels[::2]) == {"--label"}
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout.strip()
    assert pairs == {
        "org.opencontainers.image.source": release.REPOSITORY,
        "org.opencontainers.image.revision": revision,
        "org.opencontainers.image.version": "0.0.0",
    }
    assert build.image_build("hermes")[-len(labels) :] == labels


@pytest.mark.parametrize("version", ["1.2.3", "0.7.0-dev.4"])
def test_a_version_tag_build_carries_the_tags_version(version):
    environ = {"GITHUB_REF_TYPE": "tag", "GITHUB_REF_NAME": f"v{version}"}
    assert build.build_version(environ) == version


@pytest.mark.parametrize(
    "environ",
    [{}, {"GITHUB_REF_TYPE": "branch", "GITHUB_REF_NAME": "v1.2.3"}],
    ids=["local", "branch"],
)
def test_any_other_build_carries_the_placeholder(environ):
    assert build.build_version(environ) == "0.0.0"


@pytest.mark.parametrize("tag", ["1.2.3", "v1.2.3-rc.1", "v0.0.0", "v1.2"])
def test_a_tag_build_with_an_invalid_version_tag_stops(tag):
    with pytest.raises(ValueError, match="Release tag must be"):
        build.build_version({"GITHUB_REF_TYPE": "tag", "GITHUB_REF_NAME": tag})


def test_the_job_that_fills_the_cache_cannot_publish_packages():
    workflow = yaml.safe_load((build.ROOT / ".github/workflows/checks.yml").read_text())
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["images"]
    assert job["if"] == "github.event_name != 'pull_request'"
    commands = "\n".join(step.get("run", "") for step in job["steps"])
    assert "tests.support.images --github-cache" in commands
    assert "--publish" not in commands
    assert "--push" not in commands
    assert not any("login-action" in step.get("uses", "") for step in job["steps"])


def test_a_release_publishes_the_builds_every_test_job_ran():
    """`publish` waits for every test job, builds nothing, and pushes the
    archives the images job saved for the tests."""
    workflow = yaml.safe_load((build.ROOT / ".github/workflows/checks.yml").read_text())
    jobs = workflow["jobs"]
    job = jobs["publish"]
    assert set(job["needs"]) == set(jobs) - {"publish", "images"}
    assert {"images"} <= {
        need for name in job["needs"] for need in [jobs[name].get("needs")] if need
    }
    commands = "\n".join(step.get("run", "") for step in job["steps"])
    assert "--publish" in commands and "docker load" in commands
    for builder in ("docker build", "buildx build", "buildx bake", "support.images"):
        assert builder not in commands
    saved = "\n".join(step.get("run", "") for step in jobs["images"]["steps"])
    for tag in release.TESTED.values():
        assert f"docker save {tag} " in saved
    assert set(release.TESTED) == set(build.TARGETS)


def test_every_e2e_part_runs_its_share_of_one_split():
    """The e2e matrix and its `--shard K/N` agree, so no part is left out."""
    workflow = yaml.safe_load((build.ROOT / ".github/workflows/checks.yml").read_text())
    job = workflow["jobs"]["e2e"]
    parts = job["strategy"]["matrix"]["part"]
    commands = "\n".join(step.get("run", "") for step in job["steps"])
    assert parts == [str(part) for part in range(1, len(parts) + 1)]
    assert f"--shard ${{{{ matrix.part }}}}/{len(parts)}" in commands


def test_the_tested_images_are_the_ones_the_suites_build():
    from tests.support import images

    assert release.TESTED == {
        "hermes": images.HERMES_IMAGE,
        "agent-runtime": images.WORKER_TAG,
        "hermes-manager": images.MANAGER_IMAGE,
    }


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
    assert build.image_build("hermes")[:2] == ["docker", "build"]
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
