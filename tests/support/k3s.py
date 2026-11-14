"""A disposable single-server k3s, and images imported into a cluster.

Uses a separate digest-pinned k3s Docker container and a temporary kubeconfig.
No existing Kubernetes context or deployment credentials are read or modified.
"""

import json
import subprocess
import time
from pathlib import Path

import yaml

from tests.support.images import WORKER_TAG, image_source
from tests.support.ownership import (
    FIXTURE,
    KUBERNETES,
    RUN,
    CommandFailed,
    ForeignResource,
    remove_fixture,
)

ROOT = Path(__file__).resolve().parents[2]
IMAGE = (
    "rancher/k3s@sha256:"
    "21ea6e0fac4e3930c2ce8aa40ddd41a8b18cb3e9a0222c030dc005e7e88c4332"
)


def command(args, timeout=30):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, "Disposable Kubernetes fixture command failed"
    return result.stdout


def build_image(args):
    """`docker build` of a fixture image; its log holds no secret, so a failure
    shows the end of it."""
    result = subprocess.run(
        ["docker", "build", "--quiet", *args],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, (
        "building a fixture image failed: " + (result.stderr[-800:])
    )


def stream_into_cluster(identifier, tag):
    """Imports the image without an intermediate copy on either side."""
    with subprocess.Popen(
        image_source(tag), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    ) as reader:
        try:
            result = subprocess.run(
                ["docker", "exec", "-i", identifier, "ctr", "images", "import", "-"],
                stdin=reader.stdout,
                capture_output=True,
                timeout=900,
            )
            status = reader.wait(timeout=60)
        finally:
            reader.kill()
    if result.returncode or status:
        raise RuntimeError(f"importing {tag} into the cluster failed")


def import_image(identifier, tag, reuse=False):
    """Imports a local Docker image into k3s; returns its digest reference.

    With `reuse`, an image the cluster already has under `tag` is kept, for
    repeated development runs on one cluster.
    """
    reference = tag if "/" in tag.split(":")[0] else "docker.io/library/" + tag
    if reuse:
        listing = command(["docker", "exec", identifier, "ctr", "images", "list"])
        for line in listing.splitlines():
            if line.split()[0] == reference:
                return reference.rsplit(":", 1)[0] + "@" + line.split()[2]
    stream_into_cluster(identifier, tag)
    listing = command(["docker", "exec", identifier, "ctr", "images", "list"])
    digest = next(
        line.split()[2] for line in listing.splitlines() if line.split()[0] == reference
    )
    assert digest.startswith("sha256:")
    pinned = reference.rsplit(":", 1)[0] + "@" + digest
    command(["docker", "exec", identifier, "ctr", "images", "tag", reference, pinned])
    return pinned


def import_worker_image(identifier, *, uid=1000, gid=1000):
    """Imports the worker for an identity; returns its digest reference.

    The default identity is the release worker under test, which the
    `worker_image` fixture provides (or CI's saved build of it). Another
    identity is built here from the worker's Dockerfile.
    """
    if (uid, gid) == (1000, 1000):
        return import_image(identifier, WORKER_TAG)
    tag = f"hermes-worker-{uid}-{gid}:test"
    build_image(
        [
            "--tag",
            tag,
            "--build-arg",
            f"HOST_UID={uid}",
            "--build-arg",
            f"HOST_GID={gid}",
            str(ROOT / "runtimes/agent-runtime"),
        ]
    )
    return import_image(identifier, tag)


FIXED_ENTRYPOINT = """#!/bin/sh
set -eu
mkdir -p /home/worker/hostkeys
cp /etc/ssh/hostkeys/ssh_host_ed25519_key /home/worker/hostkeys/ssh_host_ed25519_key
chmod 600 /home/worker/hostkeys/ssh_host_ed25519_key
exec /usr/sbin/sshd -D -e
"""


def import_fixed_worker_image(identifier, temporary):
    """Builds a custom worker image that keeps the `worker` identity it was built
    with (1000:1000) and never binds the account to the identity it runs as;
    returns its digest reference."""
    base, tag = "hermes-worker-fixed-base:test", "hermes-worker-fixed:test"
    build_image(
        [
            "--tag",
            base,
            str(ROOT / "runtimes/agent-runtime"),
        ]
    )
    context = Path(temporary) / "fixed-worker"
    context.mkdir()
    (context / "Dockerfile").write_text(
        f"FROM {base}\nUSER root\nCOPY entrypoint.sh /entrypoint.sh\n"
        "RUN chmod 755 /entrypoint.sh\nUSER worker\n"
    )
    (context / "entrypoint.sh").write_text(FIXED_ENTRYPOINT)
    build_image(["--tag", tag, str(context)])
    return import_image(identifier, tag)


def start_k3s(network, name, run):
    """Starts a digest-pinned k3s server container labelled with its run;
    returns its ID."""
    return command(
        [
            "docker",
            "run",
            "--detach",
            "--name",
            name,
            "--privileged",
            "--network",
            network,
            "--tmpfs",
            "/run",
            "--tmpfs",
            "/var/run",
            "--label",
            f"{FIXTURE}={KUBERNETES}",
            "--label",
            f"{RUN}={run}",
            "--volume",
            f"{ROOT / 'tests/support/k3s-registries.yaml'}"
            ":/etc/rancher/k3s/registries.yaml:ro",
            "--publish",
            "127.0.0.1::6443",
            IMAGE,
            "server",
            "--disable",
            "traefik,servicelb,metrics-server",
            "--tls-san",
            "127.0.0.1",
        ],
        timeout=120,
    ).strip()


def wait_ready(identifier):
    """Waits for the API server; returns a kubeconfig reaching it from here.

    The kubeconfig holds fixture credentials: keep it in memory or private
    temporary files only.
    """
    state = json.loads(command(["docker", "inspect", identifier]))[0]
    expected_image = command(
        ["docker", "image", "inspect", IMAGE, "--format", "{{.Id}}"]
    ).strip()
    assert state["Image"] == expected_image
    port = state["NetworkSettings"]["Ports"]["6443/tcp"][0]
    assert port["HostIp"] == "127.0.0.1"
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            probe = subprocess.run(
                [
                    "docker",
                    "exec",
                    identifier,
                    "/bin/kubectl",
                    "--kubeconfig",
                    "/etc/rancher/k3s/k3s.yaml",
                    "--request-timeout=3s",
                    "get",
                    "--raw",
                    "/readyz",
                ],
                capture_output=True,
                timeout=5,
            )
        except subprocess.TimeoutExpired:
            probe = None
        if (
            probe is not None
            and probe.returncode == 0
            and probe.stdout.strip() == b"ok"
        ):
            break
        state = json.loads(command(["docker", "inspect", identifier]))[0]
        assert state["State"]["Running"], "Disposable Kubernetes server stopped"
        time.sleep(0.5)
    else:
        raise AssertionError("Disposable Kubernetes server did not become ready")
    # This output includes fixture credentials. Keep it in memory/files only.
    config = yaml.safe_load(
        command(["docker", "exec", identifier, "cat", "/etc/rancher/k3s/k3s.yaml"])
    )
    config["clusters"][0]["cluster"]["server"] = f"https://127.0.0.1:{port['HostPort']}"
    config["contexts"][0]["name"] = "personal-hermes-manager-tests"
    config["current-context"] = "personal-hermes-manager-tests"
    return config


def remove_container(identifier, run=None):
    """Removes exactly this fixture container, after proving it is one."""
    # Docker can report a transient removal conflict while the server
    # finishes exiting, or lose the CLI's response after removing it. Retry
    # only this fixture's exact ID and observe its absence; a daemon failure
    # is never treated as proof of absence.
    for attempt in range(3):
        try:
            remove_fixture(identifier, KUBERNETES, run)
            return
        except ForeignResource:
            raise
        except CommandFailed, subprocess.TimeoutExpired:
            pass
        remaining = command(
            [
                "docker",
                "ps",
                "--all",
                "--no-trunc",
                "--filter",
                f"id={identifier}",
                "--format",
                "{{.ID}}",
            ]
        )
        if not remaining.strip():
            return
        assert attempt < 2, "Disposable Kubernetes fixture cleanup failed"
        time.sleep(0.2)
