"""Exercise public release metadata without Docker, network or deployment secrets."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from agent_platform import release

pytestmark = pytest.mark.unit


def test_packaged_chart_has_pins_and_wiring_without_modifying_source(tmp_path):
    images = {
        name: release.REGISTRY + "/" + name + "@sha256:" + "a" * 64
        for name in release.TARGETS
    }
    original = (release.ROOT / "charts/agent-platform/values.yaml").read_bytes()
    chart = release.prepare_chart(tmp_path, images, "0.1.0", "b" * 40)
    assert yaml.safe_load((chart / "values.yaml").read_text())["images"] == images
    definition = yaml.safe_load((chart / "Chart.yaml").read_text())
    assert definition["version"] == "0.1.0"
    assert definition["annotations"]["org.opencontainers.image.revision"] == "b" * 40
    assert (chart / "templates/hermes-manager.yaml").is_file()
    assert not (tmp_path / "contract.zip").exists()
    assert (release.ROOT / "charts/agent-platform/values.yaml").read_bytes() == original


@pytest.mark.parametrize(
    "version",
    ["latest", "v0.1.0", "01.1.0", "../escape", "0.1", "0.1.0-beta", "0.7.0-dev.01"],
)
def test_invalid_version_never_runs_build_or_publish_commands(version):
    with patch.object(release, "run") as run:
        for action in (release.build, release.publish):
            with pytest.raises(ValueError, match="semantic version"):
                action(version, "a" * 40)
        run.assert_not_called()


def test_stable_and_development_versions_are_distinct():
    for version in ("0.6.9", "0.7.0", "0.7.0-dev.0", "0.7.0-dev.12"):
        assert release.VERSION.fullmatch(version), version
    assert not release.VERSION.fullmatch("0.7.0-rc.1")


LABELS = {
    "org.opencontainers.image.source": release.REPOSITORY,
    "org.opencontainers.image.revision": "a" * 40,
}


def publishing(tmp_path, version, labels=None, access=None, commands=None):
    """Runs `publish` with Docker, Helm and GHCR replaced; returns the commands."""
    commands = [] if commands is None else commands

    def run(args, **kwargs):
        commands.append(args)
        if args[:3] == ["docker", "image", "inspect"]:
            found = labels
            if found is None:
                found = {**LABELS, "org.opencontainers.image.version": version}
            return json.dumps(found).encode()
        if args[:2] == ["docker", "push"]:
            return f"{args[2]}: digest: sha256:{'c' * 64} size: 1234".encode()
        if args[:2] == ["helm", "push"]:
            return b"Digest: sha256:" + b"b" * 64
        return b""

    with (
        patch.object(release, "OUTPUT", tmp_path),
        patch.object(release, "verify_release_source"),
        patch.object(release, "run", side_effect=run),
        patch.object(release, "render_chart"),
        patch.object(
            release, "require_push_access", side_effect=access or (lambda names: None)
        ),
    ):
        receipt = release.publish(version, "a" * 40)
    return receipt, commands


def test_the_tested_images_are_pushed_and_the_chart_last(tmp_path):
    receipt, commands = publishing(tmp_path, "0.1.0")
    assert receipt["schema"] == 4
    assert receipt["version"] == "0.1.0"
    assert receipt["source"]["repository"] == release.REPOSITORY
    assert receipt["images"] == {
        name: f"{release.REGISTRY}/{name}@sha256:{'c' * 64}" for name in release.TARGETS
    }
    assert commands[-1][:2] == ["helm", "push"]
    tags = [command[2:] for command in commands if command[:2] == ["docker", "tag"]]
    assert tags == [
        [release.TESTED[name], f"{release.REGISTRY}/{name}:0.1.0"]
        for name in release.TARGETS
    ]
    builds = ("build", "bake")
    assert not any(
        command[:2] == ["docker", "build"] or command[2:3] in [[b] for b in builds]
        for command in commands
    )
    promotions = [
        command
        for command in commands
        if command[:3] == ["docker", "buildx", "imagetools"]
    ]
    assert len(promotions) == len(release.TARGETS)
    assert all(
        not any("pulumi" in part.lower() for part in command) for command in commands
    )


def test_a_development_release_never_moves_latest(tmp_path):
    _receipt, commands = publishing(tmp_path, "0.1.0-dev.1")
    assert not any(
        command[:3] == ["docker", "buildx", "imagetools"] for command in commands
    )
    assert commands[-1][:2] == ["helm", "push"]


@pytest.mark.parametrize(
    "labels",
    [
        {**LABELS, "org.opencontainers.image.version": "0.0.0"},
        {**LABELS, "org.opencontainers.image.revision": "d" * 40},
        {},
    ],
    ids=["placeholder-version", "other-commit", "unlabelled"],
)
def test_an_image_built_from_another_source_is_never_published(tmp_path, labels):
    with pytest.raises(ValueError, match="not this release's build"):
        publishing(tmp_path, "0.1.0", labels=labels)


def test_missing_push_access_stops_before_the_first_push(tmp_path):
    def refuse(names):
        assert len(names) == len(release.TARGETS) + 1
        raise ValueError("GHCR refuses pushes")

    commands = []
    with pytest.raises(ValueError, match="refuses pushes"):
        publishing(tmp_path, "0.1.0", access=refuse, commands=commands)
    assert commands
    assert not any(
        command[:2] in (["docker", "push"], ["docker", "tag"]) for command in commands
    )


class Response:
    def __init__(self, status, body=b"", location=""):
        self.status, self.body, self.headers = status, body, {"Location": location}

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exception):
        return False


def test_push_access_opens_and_cancels_an_upload_with_the_credential_in_headers(
    monkeypatch,
):
    monkeypatch.setenv("GITHUB_ACTOR", "fixture-actor")
    monkeypatch.setenv("GHCR_TOKEN", "fixture-registry-credential")
    requests = []

    def urlopen(request, timeout):
        requests.append(request)
        if "/token?" in request.full_url:
            return Response(200, json.dumps({"token": "fixture-bearer"}).encode())
        if request.get_method() == "POST":
            return Response(202, location="/v2/example/upload/fixture-session")
        return Response(204)

    with patch.object(release.urllib.request, "urlopen", side_effect=urlopen):
        release.require_push_access(["example/one", "example/two"])
    assert [request.get_method() for request in requests] == [
        "GET",
        "POST",
        "DELETE",
    ] * 2
    assert requests[2].full_url == "https://ghcr.io/v2/example/upload/fixture-session"
    assert all("fixture-registry-credential" not in r.full_url for r in requests)
    assert "push" in requests[0].full_url


def test_a_refused_upload_refuses_the_release(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTOR", "fixture-actor")
    monkeypatch.setenv("GHCR_TOKEN", "fixture-registry-credential")

    def urlopen(request, timeout):
        if "/token?" in request.full_url:
            return Response(200, json.dumps({"token": "fixture-bearer"}).encode())
        raise release.urllib.error.HTTPError(request.full_url, 403, "denied", {}, None)

    with patch.object(release.urllib.request, "urlopen", side_effect=urlopen):
        with pytest.raises(ValueError, match="refuses pushes to example/one"):
            release.require_push_access(["example/one"])


def test_publishing_only_runs_for_version_tags_on_hosted_runners():
    workflow = yaml.safe_load(
        (release.ROOT / ".github/workflows/checks.yml").read_text()
    )
    job = workflow["jobs"]["publish"]
    assert workflow["on"]["push"]["tags"]
    assert "startsWith(github.ref, 'refs/tags/v')" in job["if"]
    assert "github.event_name == 'push'" in job["if"]
    assert job["runs-on"] == "ubuntu-latest"
    assert job["permissions"]["packages"] == "write"
    assert workflow["permissions"] == {"contents": "read"}
    commands = "\n".join(step.get("run", "") for step in job["steps"])
    assert "pulumi" not in commands.lower()
    assert "GITHUB_REF_NAME" in commands


def test_failed_build_diagnostics_are_explicit_and_bounded(tmp_path):
    """Only opted-in generic build output is retained, with a strict size bound."""
    import sys

    from agent_platform.command import run

    path = tmp_path / "build.log"
    with pytest.raises(RuntimeError) as error:
        run(
            [
                sys.executable,
                "-c",
                (
                    'import sys; print("x" * 3_000_000); '
                    'print("build failed", file=sys.stderr); sys.exit(2)'
                ),
            ],
            diagnostics=path,
        )
    assert path.stat().st_size == 2_097_152
    assert path.read_bytes().endswith(b"build failed\n")
    assert "build failed" not in str(error.value)


def test_reviewed_build_sources_are_complete_and_not_ignored():
    """Generic credential ignore patterns must not drop required source files."""
    import subprocess

    manifest = json.loads((release.ROOT / "release-policy.json").read_text())
    names = []
    for context in manifest["contexts"].values():
        for relative in context.get("files", []):
            name = str(Path(context["root"]) / relative)
            assert (release.ROOT / name).is_file(), name
            names.append(name)
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin", "-z"],
        input=("\0".join(names) + "\0").encode(),
        capture_output=True,
        cwd=release.ROOT,
        timeout=10,
    )
    assert result.returncode == 1, result.stdout.decode()


def test_this_product_publishes_under_its_own_names():
    """agent-platform-v2 never pushes to the public agent-platform packages."""
    chart = yaml.safe_load(
        (release.ROOT / "charts/agent-platform/Chart.yaml").read_text()
    )
    assert chart["name"] == release.CHART == "agent-platform-v2"
    assert release.REPOSITORY == "https://github.com/Accelero/agent-platform-v2"
    assert chart["sources"] == [release.REPOSITORY]
    assert chart["annotations"]["org.opencontainers.image.source"] == release.REPOSITORY
    assert release.REGISTRY == "ghcr.io/accelero/agent-platform-v2"
    assert release.CHART_REGISTRY == f"oci://{release.REGISTRY}/charts"
    bake = (release.ROOT / "containers/docker-bake.hcl").read_text()
    manager = (release.ROOT / "services/hermes-manager/Dockerfile").read_text()
    for text in (bake, manager):
        assert release.REPOSITORY + '"' in text
        assert 'Accelero/agent-platform"' not in text
    assert f'default = "{release.REGISTRY}"' in bake
    values = yaml.safe_load(
        (release.ROOT / "charts/agent-platform/values.yaml").read_text()
    )
    assert all(
        image.startswith(release.REGISTRY + "/") for image in values["images"].values()
    )
