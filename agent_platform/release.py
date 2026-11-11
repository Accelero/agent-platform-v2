"""Build versioned agent artifacts locally, or publish the tested ones.

A release tag runs every test job of `Checks` on images built once from the
tagged commit; only when all of them passed does its `publish` job push those
same images and the chart that pins their digests. No deployment credentials
are involved.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

from agent_platform.build import REPOSITORY, ROOT, TARGETS, bake_arguments
from agent_platform.command import run
from agent_platform.privacy import chart_files, stage_contexts

OUTPUT = ROOT / ".local/releases"
# agent-platform-v2 is its own product: its packages, chart and versions are
# separate from those of the public agent-platform repository.
REGISTRY = "ghcr.io/accelero/agent-platform-v2"
CHART_REGISTRY = "oci://" + REGISTRY + "/charts"
CHART = "agent-platform-v2"
# The image each release target's tests ran, as the `images` job of `Checks`
# builds and saves it.
TESTED = {
    "hermes": "personal-hermes-native-test:local",
    "agent-runtime": "hermes-next-worker:test",
    "hermes-manager": "hermes-manager:e2e",
}
# MAJOR.MINOR.PATCH, or a development prerelease MAJOR.MINOR.PATCH-dev.N that
# never collides with a stable version and is never resolved as the latest one.
VERSION = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-dev\.(?:0|[1-9][0-9]*))?"
)


# Untagged commits carry this in the chart and the project; only a tagged
# release commit carries the tag's version.
PLACEHOLDER = "0.0.0"
VERSION_RULE = "Release tag must be vMAJOR.MINOR.PATCH or vMAJOR.MINOR.PATCH-dev.N"


def source_versions(root: Path = ROOT) -> dict[str, str]:
    """The versions the source tree declares."""
    chart = yaml.safe_load((root / "charts/agent-platform/Chart.yaml").read_text())
    project = tomllib.loads((root / "pyproject.toml").read_text())
    return {
        "chart": str(chart["version"]),
        "chart appVersion": str(chart["appVersion"]),
        "project": str(project["project"]["version"]),
    }


def verify_release_source(version: str, root: Path = ROOT) -> None:
    """A release commit declares exactly the tag's version in chart and project.

    The placeholder is never a release: it names unreleased source.
    """
    if not VERSION.fullmatch(version) or version == PLACEHOLDER:
        raise ValueError(VERSION_RULE)
    declared = source_versions(root)
    for name in ("chart", "project"):
        if declared[name] != version:
            raise ValueError(f"The {name} version must equal the release tag.")


def verify_placeholder_source(root: Path = ROOT) -> None:
    """Ordinary commits keep the placeholder everywhere."""
    for name, found in source_versions(root).items():
        if found != PLACEHOLDER:
            raise ValueError(f"The {name} version must stay {PLACEHOLDER}.")


def prepare_chart(
    target: Path, images: dict[str, str], version: str, revision: str
) -> Path:
    """Package all executable wiring and exact runtime pins in the chart itself."""
    chart = target / "chart/agent-platform"
    for name in chart_files(ROOT):
        source = ROOT / "charts/agent-platform" / name
        destination = chart / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
    values = yaml.safe_load((chart / "values.yaml").read_text())
    values["images"] = images
    (chart / "values.yaml").write_text(yaml.safe_dump(values, sort_keys=False))
    definition = yaml.safe_load((chart / "Chart.yaml").read_text())
    definition.update(version=version, appVersion=version)
    definition["annotations"].update(
        {
            "org.opencontainers.image.revision": revision,
            "agent-platform.io/configuration-version": "2",
        }
    )
    (chart / "Chart.yaml").write_text(yaml.safe_dump(definition, sort_keys=False))
    return chart


def render_chart(target: Path, chart: Path) -> None:
    """Exercise the nonsecret manager example without personal deployment inputs."""
    text = (ROOT / "examples/manager/values.yaml").read_text()
    values = target / "reviewed-values.yaml"
    values.write_text(text)
    output = run(
        [
            "helm",
            "template",
            "agents",
            str(chart),
            "--namespace",
            "agents",
            "--values",
            str(values),
        ],
        timeout=60,
    )
    documents = [item for item in yaml.safe_load_all(output) if item]
    deployed = {
        item["metadata"]["name"]
        for item in documents
        if item["kind"] in {"Deployment", "StatefulSet", "DaemonSet"}
    }
    if deployed != {
        "hermes-manager-api",
        "hermes-manager-controller",
        "hermes-gateway",
    }:
        raise ValueError("The manager example contains deferred or missing workloads.")
    for item in documents:
        if item["kind"] == "PersistentVolumeClaim":
            raise ValueError("The manager example must not create claims.")
        if item["kind"] == "Deployment":
            volumes = item["spec"]["template"]["spec"].get("volumes", [])
            if len({value["name"] for value in volumes}) != len(volumes):
                raise ValueError("The chart generated duplicate volume names.")
    (target / "reviewed-manifests.yaml").write_bytes(output)


def _require_names(version: str, revision: str, *registries: str) -> None:
    if (
        not VERSION.fullmatch(version)
        or version == PLACEHOLDER
        or not re.fullmatch(r"[a-f0-9]{40}", revision)
    ):
        raise ValueError("Use a complete semantic version and source commit SHA.")
    image_registry, chart_registry = registries
    if not re.fullmatch(
        r"ghcr\.io/[a-z0-9][a-z0-9/_-]*", image_registry
    ) or not re.fullmatch(r"oci://ghcr\.io/[a-z0-9][a-z0-9/_-]*", chart_registry):
        raise ValueError("Use explicit lowercase GHCR namespaces.")


def verify_images(images: dict[str, str]) -> None:
    """The release properties of each image, checked offline before publication."""
    run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--user",
            "0:0",
            "--entrypoint",
            "python3",
            images["agent-runtime"],
            "-c",
            (
                "from pathlib import Path; "
                "assert not list(Path('/etc/ssh').glob('ssh_host_*_key')); "
                "assert not Path('/etc/ssl/private/ssl-cert-snakeoil.key').exists()"
            ),
        ],
        timeout=60,
    )
    run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--interactive",
            "--entrypoint",
            "/opt/hermes/.venv/bin/python",
            images["hermes"],
            "-",
        ],
        data=(ROOT / "tests/hermes_execution_smoke.py").read_bytes(),
        timeout=180,
    )
    run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--entrypoint",
            "sh",
            images["hermes-manager"],
            "-c",
            'test "$(id -u)" = 10001 && '
            "test -s /usr/share/hermes-manager/ui/index.html && "
            "test -x /usr/local/bin/hermes-manager && "
            "test -x /usr/local/bin/hermes-gateway && ! command -v node",
        ],
        timeout=60,
    )


def package_chart(
    target: Path, images: dict[str, str], version: str, revision: str
) -> Path:
    """Lint, render and package the chart that pins `images`."""
    sys.stdout.write("Validating chart.\n")
    sys.stdout.flush()
    chart = prepare_chart(target, images, version, revision)
    run(["helm", "lint", str(chart)], timeout=60)
    render_chart(target, chart)
    run(
        [
            "helm",
            "package",
            str(chart),
            "--version",
            version,
            "--app-version",
            version,
            "--destination",
            str(target),
        ],
        timeout=60,
    )
    return target / f"{CHART}-{version}.tgz"


def receipt_for(version: str, revision: str, images: dict[str, str]) -> dict:
    return {
        "schema": 4,
        "source": {"repository": REPOSITORY, "revision": revision},
        "version": version,
        "images": images,
    }


def build(
    version: str,
    revision: str,
    *,
    registry: str = REGISTRY,
    chart_registry: str = CHART_REGISTRY,
) -> dict:
    """Build, verify and package a release locally; nothing leaves the machine."""
    _require_names(version, revision, registry, chart_registry)
    target = OUTPUT / version
    target.mkdir(parents=True, exist_ok=True)
    contexts = stage_contexts(ROOT, target / "contexts")
    args = [
        *bake_arguments(contexts=contexts),
        "--load",
        "--set",
        "*.labels.org.opencontainers.image.revision=" + revision,
        "--set",
        "*.labels.org.opencontainers.image.version=" + version,
    ]
    for name in TARGETS:
        args.extend(["--set", f"{name}.tags={registry}/{name}:{version}"])
    sys.stdout.write("Building agent release " + version + ".\n")
    sys.stdout.flush()
    run(args, cwd=ROOT, timeout=5400, diagnostics=target / "build.log")
    images = {name: f"{registry}/{name}:{version}" for name in TARGETS}
    sys.stdout.write("Verifying built runtime images.\n")
    sys.stdout.flush()
    verify_images(images)
    package = package_chart(target, images, version, revision)
    receipt = receipt_for(version, revision, images)
    receipt["chart"] = str(package.relative_to(ROOT))
    (target / "artifacts.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def require_tested_images(version: str, revision: str) -> None:
    """Each tested image was built from this commit, labelled for this version."""
    inspect = ["docker", "image", "inspect", "--format", "{{json .Config.Labels}}"]
    for name, tag in TESTED.items():
        labels = json.loads(run([*inspect, tag], timeout=60))
        expected = {
            "org.opencontainers.image.source": REPOSITORY,
            "org.opencontainers.image.revision": revision,
            "org.opencontainers.image.version": version,
        }
        if any((labels or {}).get(key) != value for key, value in expected.items()):
            raise ValueError(f"The tested {name} image is not this release's build.")


def _ghcr(request: urllib.request.Request) -> int:
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def require_push_access(repositories: list[str]) -> None:
    """Refuse before the first push when any one repository would refuse it.

    Opening, then cancelling, a blob upload proves the push permission without
    publishing anything, so a release is never left half published for lack
    of access. The credential stays in the environment.
    """
    actor, token = os.environ.get("GITHUB_ACTOR"), os.environ.get("GHCR_TOKEN")
    if not actor or not token:
        raise ValueError("Publishing requires GITHUB_ACTOR and GHCR_TOKEN.")
    basic = base64.b64encode(f"{actor}:{token}".encode()).decode()
    for repository in repositories:
        scope = urllib.parse.quote(f"repository:{repository}:pull,push")
        request = urllib.request.Request(
            f"https://ghcr.io/token?service=ghcr.io&scope={scope}",
            headers={"Authorization": "Basic " + basic},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                bearer = json.loads(response.read())["token"]
        except urllib.error.URLError, KeyError, ValueError:
            raise ValueError(f"GHCR gave no push token for {repository}.") from None
        authorization = {"Authorization": "Bearer " + bearer}
        upload = urllib.request.Request(
            f"https://ghcr.io/v2/{repository}/blobs/uploads/",
            method="POST",
            headers=authorization,
        )
        try:
            with urllib.request.urlopen(upload, timeout=30) as response:
                location = response.headers.get("Location", "")
        except urllib.error.URLError:
            raise ValueError(f"GHCR refuses pushes to {repository}.") from None
        if location:
            cancel = urllib.parse.urljoin("https://ghcr.io/", location)
            _ghcr(
                urllib.request.Request(cancel, method="DELETE", headers=authorization)
            )


def publish(
    version: str,
    revision: str,
    *,
    registry: str = REGISTRY,
    chart_registry: str = CHART_REGISTRY,
) -> dict:
    """Publish the images the tests ran, and their chart last.

    Nothing is rebuilt: the images are the ones the test jobs of the same run
    loaded, and this runs only after all of them passed.
    """
    _require_names(version, revision, registry, chart_registry)
    verify_release_source(version)
    target = OUTPUT / version
    target.mkdir(parents=True, exist_ok=True)
    require_tested_images(version, revision)
    sys.stdout.write("Verifying the tested runtime images.\n")
    sys.stdout.flush()
    verify_images(TESTED)
    names = [f"{registry}/{name}" for name in TARGETS]
    charts = chart_registry.removeprefix("oci://") + "/" + CHART
    require_push_access([name.removeprefix("ghcr.io/") for name in [*names, charts]])
    images = {}
    for name in TARGETS:
        reference = f"{registry}/{name}:{version}"
        run(["docker", "tag", TESTED[name], reference], timeout=60)
        sys.stdout.write(f"Publishing {name} {version}.\n")
        sys.stdout.flush()
        output = run(["docker", "push", reference], timeout=1800)
        digests = re.findall(rb"digest: (sha256:[a-f0-9]{64})", output)
        if len(digests) != 1:
            raise ValueError("The registry returned no manifest digest.")
        images[name] = f"{registry}/{name}@{digests[0].decode()}"
    package = package_chart(target, images, version, revision)
    receipt = receipt_for(version, revision, images)
    if "-dev." not in version:
        for name, image in images.items():
            run(
                [
                    "docker",
                    "buildx",
                    "imagetools",
                    "create",
                    "--tag",
                    f"{registry}/{name}:latest",
                    image,
                ],
                timeout=180,
            )
    output = run(
        ["helm", "push", str(package), chart_registry],
        timeout=180,
        include_stderr=True,
    )
    matches = re.findall(rb"Digest:\s*(sha256:[a-f0-9]{64})", output)
    if len(matches) != 1:
        raise ValueError("Helm did not return one chart manifest digest.")
    receipt["chart"] = f"{chart_registry}/{CHART}@{matches[0].decode()}"
    (target / "artifacts.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def main() -> int:
    """Require an explicit release version; infrastructure never invokes builds."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--registry", default=REGISTRY)
    parser.add_argument("--chart-registry", default=CHART_REGISTRY)
    parser.add_argument(
        "--publish",
        action="store_true",
        help="push the tested images that are loaded, instead of building",
    )
    args = parser.parse_args()
    try:
        receipt = (publish if args.publish else build)(
            args.version,
            args.revision,
            registry=args.registry,
            chart_registry=args.chart_registry,
        )
    except OSError, ValueError, RuntimeError:
        sys.stderr.write("Agent release failed; private command output suppressed.\n")
        return 1
    sys.stdout.write(json.dumps(receipt, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
