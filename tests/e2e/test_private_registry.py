"""A terminal backend whose worker image is in a private registry.

The registry runs behind basic authentication on the platform's Docker
network and holds the release worker under test. The case changes Bob's
backends, so it takes a platform of the destructive pool.
"""

import base64
import io
import json
import subprocess
import tarfile
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest
import requests

from tests.support import system
from tests.support.images import WORKER_TAG, image_source
from tests.support.k3s import command
from tests.support.ownership import (
    FIXTURE,
    REGISTRY_FIXTURE,
    RUN,
    Cleanup,
    remove_fixture,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.cluster,
    pytest.mark.destructive,
]
REGISTRY_PROXY_IMAGE = (
    "python@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90"
)


REGISTRY = "registry.e2e.test:5000"


REGISTRY_IMAGE = (
    "registry@sha256:ddf754342cfc8acc51a56d5d0ab6af06826461864460636d8bd5c546dab2a7b8"
)


PRIVATE_SCRIPT = (
    system.PLUGIN_PRELUDE
    + """
result = {}
try:
    private = environment("private", "e2e-private-TASK")
    reply = private.execute("id -u")
    result["output"], result["returncode"] = reply["output"], reply["returncode"]
    private.cleanup()
except Exception as error:
    result["error"] = str(error)
print(json.dumps(result))
"""
)


def push_image(tag, base, repository, credentials):
    """Uploads a local image's linux/amd64 manifest and blobs through the
    registry API (Docker refuses plain HTTP pushes); returns its digest."""
    archive = subprocess.run(
        image_source(tag), capture_output=True, timeout=600, check=True
    ).stdout
    bundle = tarfile.open(fileobj=io.BytesIO(archive))

    def blob(digest):
        return bundle.extractfile("blobs/sha256/" + digest.split(":", 1)[1]).read()

    session = requests.Session()
    session.auth = credentials

    def manifest(descriptor):
        document = json.loads(blob(descriptor["digest"]))
        for child in document.get("manifests", []):
            platform = child.get("platform", {})
            if (platform.get("os"), platform.get("architecture")) == ("linux", "amd64"):
                return manifest(child)
        assert "layers" in document, "no linux/amd64 image manifest"
        return descriptor, document

    index = json.loads(bundle.extractfile("index.json").read())
    descriptor, document = manifest(index["manifests"][0])
    for layer in [document["config"], *document["layers"]]:
        digest = layer["digest"]
        if session.head(f"{base}/v2/{repository}/blobs/{digest}", timeout=30).ok:
            continue
        started = session.post(f"{base}/v2/{repository}/blobs/uploads/", timeout=30)
        assert started.status_code == 202, f"blob upload: {started.status_code}"
        location = started.headers["Location"]
        if not location.startswith("http"):
            location = base + location
        stored = session.put(
            location,
            params={"digest": digest},
            data=blob(digest),
            headers={"Content-Type": "application/octet-stream"},
            timeout=300,
        )
        assert stored.status_code == 201, f"blob: {stored.status_code}"
    stored = session.put(
        f"{base}/v2/{repository}/manifests/{descriptor['digest']}",
        data=blob(descriptor["digest"]),
        headers={"Content-Type": document.get("mediaType") or descriptor["mediaType"]},
        timeout=60,
    )
    assert stored.status_code == 201, f"manifest: {stored.status_code}"
    return descriptor["digest"]


@contextmanager
def private_registry(identifier, network, temporary, password, run):
    """A registry behind basic authentication on the fixture network holding
    the worker image; yields the image's digest reference there. The node
    resolves the registry's name through its hosts file: the default bridge
    network has no container DNS."""
    suffix = uuid.uuid4().hex[:8]
    upstream = f"personal-hermes-registry-{suffix}"
    proxy = f"personal-hermes-registry-proxy-{suffix}"
    labels = [
        "--label",
        f"{FIXTURE}={REGISTRY_FIXTURE}",
        "--label",
        f"{RUN}={run}",
    ]
    command(["docker", "pull", "--quiet", REGISTRY_IMAGE], timeout=600)
    command(["docker", "pull", "--quiet", REGISTRY_PROXY_IMAGE], timeout=600)
    with Cleanup() as cleanup:
        command(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                upstream,
                *labels,
                "--network",
                network,
                REGISTRY_IMAGE,
            ]
        )
        cleanup.add(upstream, lambda: remove_fixture(upstream, REGISTRY_FIXTURE, run))
        settings = Path(temporary) / "registry.env"
        settings.write_text(
            f"UPSTREAM={system.container_ip(upstream, network)}:5000\n"
            f"CREDENTIALS=e2e:{password}\n"
        )
        settings.chmod(0o600)
        command(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                proxy,
                *labels,
                "--network",
                network,
                "--env-file",
                str(settings),
                "--volume",
                f"{system.HERE / 'registry_proxy.py'}:/proxy/registry_proxy.py:ro",
                "--entrypoint",
                "python",
                "--publish",
                "127.0.0.1::5000",
                REGISTRY_PROXY_IMAGE,
                "/proxy/registry_proxy.py",
            ]
        )
        cleanup.add(proxy, lambda: remove_fixture(proxy, REGISTRY_FIXTURE, run))
        host = REGISTRY.split(":")[0]
        address = system.container_ip(proxy, network)
        command(
            [
                "docker",
                "exec",
                identifier,
                "sh",
                "-c",
                # Replaces an earlier run's entry on a reused cluster.
                f"kept=$(grep -v ' {host}$' /etc/hosts); "
                f"printf '%s\\n%s\\n' \"$kept\" '{address} {host}'"
                " > /etc/hosts",
            ]
        )
        state = json.loads(command(["docker", "inspect", proxy]))[0]
        port = state["NetworkSettings"]["Ports"]["5000/tcp"][0]["HostPort"]
        base = f"http://127.0.0.1:{port}"
        system.wait(
            lambda: requests.get(f"{base}/v2/", timeout=3).status_code == 401,
            60,
            "the private registry did not start",
            interval=1,
        )
        digest = push_image(WORKER_TAG, base, "hermes-worker", ("e2e", password))
        yield f"{REGISTRY}/hermes-worker@{digest}"


def a_private_backend_pulls_with_its_registry_secret_and_reports_failures(
    identifier, admin, assignments, images, passwords, worker
):
    """A backend from a private registry: wrong credentials are reported as
    `image_unavailable` and never replaced by another image; after the
    registry secret is corrected the worker pulls and runs."""
    instance = assignments["bob"]["instanceId"]
    namespace = admin.phase(instance)["namespace"]

    def registry_secret(password):
        auth = base64.b64encode(f"e2e:{password}".encode()).decode()
        admin.call(
            "PUT",
            "/api/v1/secrets/e2e-registry",
            {
                "kind": "registry",
                "value": json.dumps({"auths": {REGISTRY: {"auth": auth}}}),
            },
        )

    def converged():
        view = admin.phase(instance)
        return (
            view["phase"] == "ready"
            and view["observedGeneration"] == view["desiredGeneration"]
        )

    registry_secret("wrong-" + passwords["registry"])
    private = admin.publish(
        "terminal-backends",
        "private",
        {
            **worker,
            "image": images["private"],
            "registrySecret": "e2e-registry",
            "limits": {"idleSeconds": 60, "maxLeaseSeconds": 900, "maxEnvironments": 2},
            "mounts": {"workspace": {"mountPath": "/workspace"}},
        },
    )
    system.change_terminal(
        admin,
        assignments["bob"]["assignmentId"],
        lambda terminal: terminal["allowed"].update(private=private),
    )
    system.wait(
        converged,
        system.READY_TIMEOUT,
        "Bob's instance did not take the backend",
        interval=5,
    )
    known = {e["id"] for e in system.environments(admin, instance)}
    result = system.hermes_python(
        identifier, namespace, PRIVATE_SCRIPT.replace("TASK", "1")
    )
    assert "(422)" in result.get("error", ""), f"wrong credentials: {result}"
    failed = [
        e["failure"]
        for e in system.environments(admin, instance)
        if e["id"] not in known and e["backend"]["id"] == "private"
    ]
    assert failed == ["image_unavailable"], failed

    registry_secret(passwords["registry"])
    system.wait(
        converged,
        system.READY_TIMEOUT,
        "Bob's instance did not take the secret",
        interval=5,
    )
    result = system.hermes_python(
        identifier, namespace, PRIVATE_SCRIPT.replace("TASK", "2")
    )
    assert result.get("returncode") == 0, f"right credentials: {result}"
    assert result["output"].split() == ["1000"], result
    pulled = command(["docker", "exec", identifier, "crictl", "images", "-o", "json"])
    assert REGISTRY + "/hermes-worker" in pulled, "the worker image was not pulled"


@pytest.fixture
def registry(destructive_platform, tmp_path):
    platform = destructive_platform
    with private_registry(
        platform.identifier,
        platform.network,
        tmp_path,
        platform.passwords["registry"],
        platform.cluster.run,
    ) as image:
        yield image


@pytest.mark.security
def test_a_private_backend_pulls_with_its_registry_secret_and_reports_failures(
    destructive_platform, registry
):
    platform = destructive_platform
    a_private_backend_pulls_with_its_registry_secret_and_reports_failures(
        platform.identifier,
        platform.admin,
        platform.assignments,
        {"private": registry},
        platform.passwords,
        platform.state.worker,
    )
