"""Run resource ownership and bootstrap tests on a disposable Kubernetes server.

Uses a separate digest-pinned k3s Docker container and a temporary kubeconfig.
No existing Kubernetes context or deployment credentials are read or modified.
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

from tests.support.ownership import (
    FIXTURE,
    KUBERNETES,
    POSTGRES,
    RUN,
    Cleanup,
    CommandFailed,
    ForeignResource,
    new_run,
    remove_fixture,
    require_fixture,
)

ROOT = Path(__file__).resolve().parents[2]
IMAGE = (
    "rancher/k3s@sha256:"
    "ca7f37d993d82ef0dcdcfecb2e0e2618ea541dbaffc620c8cedebe01a82acd0d"
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


WORKER_TAG = "hermes-next-worker:test"


def image_source(tag):
    """The command that writes the image's archive: CI's saved build, or Docker."""
    directory = os.environ.get("HERMES_TEST_IMAGE_ARCHIVES")
    if directory:
        name = tag.replace("/", "_").replace(":", "_") + ".tar.zst"
        if (Path(directory) / name).exists():
            return ["zstd", "-dc", str(Path(directory) / name)]
    return ["docker", "save", tag]


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
    """Builds the requested worker identity and imports a digest reference."""
    tag = (
        WORKER_TAG if (uid, gid) == (1000, 1000) else f"hermes-worker-{uid}-{gid}:test"
    )
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
            f"{ROOT / 'tests/personal_hermes/k3s-registries.yaml'}"
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres-container", default="personal-hermes-manager-tests")
    parser.add_argument(
        "--suite",
        action="append",
        required=True,
        help="a verification suite to run on this cluster; repeatable",
    )
    parser.add_argument(
        "--k3d",
        action="store_true",
        help="Use a disposable k3d cluster without touching a deployment context",
    )
    args = parser.parse_args()
    # Injection (pausing the database) and database creation happen in this
    # container: prove it is a fixture before anything else.
    require_fixture(args.postgres_container, POSTGRES)
    postgres = json.loads(command(["docker", "inspect", args.postgres_container]))[0]
    networks = postgres["NetworkSettings"]["Networks"]
    assert networks, "Disposable PostgreSQL fixture has no network"
    network = "bridge" if "bridge" in networks else next(iter(networks))
    run = new_run()
    name = "personal-hermes-kube-test-" + run
    if args.k3d:
        name = "hermes-native-" + run[:8]
    with Cleanup() as cleanup:
        if args.k3d:
            with socket.socket() as port_socket:
                port_socket.bind(("127.0.0.1", 0))
                api_port = port_socket.getsockname()[1]
            cleanup.add(
                "k3d cluster",
                lambda: command(["k3d", "cluster", "delete", name], timeout=90),
            )
            command(
                [
                    "k3d",
                    "cluster",
                    "create",
                    name,
                    "--image",
                    IMAGE,
                    "--api-port",
                    f"127.0.0.1:{api_port}",
                    "--no-lb",
                    "--kubeconfig-update-default=false",
                    "--kubeconfig-switch-context=false",
                    "--wait",
                    "--timeout",
                    "120s",
                ],
                timeout=180,
            )
            identifier = command(
                ["docker", "inspect", "--format", "{{.Id}}", f"k3d-{name}-server-0"]
            ).strip()
            cleanup.add(
                "database network membership",
                lambda: command(
                    [
                        "docker",
                        "network",
                        "disconnect",
                        f"k3d-{name}",
                        args.postgres_container,
                    ]
                ),
            )
            command(
                ["docker", "network", "connect", f"k3d-{name}", args.postgres_container]
            )
        else:
            identifier = start_k3s(network, name, run)
            cleanup.add("k3s container", lambda: remove_container(identifier, run))
        config = wait_ready(identifier)
        postgres_port = (
            command(["docker", "port", args.postgres_container, "5432/tcp"])
            .strip()
            .splitlines()[0]
            .rsplit(":", 1)[1]
        )
        with tempfile.TemporaryDirectory(
            prefix="personal-hermes-kubernetes-"
        ) as temporary:
            path = Path(temporary) / "kubeconfig"
            path.write_text(yaml.safe_dump(config))
            path.chmod(0o600)
            environment = dict(os.environ)
            environment["HERMES_TEST_KUBECONFIG"] = str(path)
            environment["HERMES_TEST_RUN"] = run
            environment["DATABASE_URL"] = (
                f"postgres://hermes_test@127.0.0.1:{postgres_port}/hermes_test"
            )
            # The rewrite's adapter tests run in-process as the controller's
            # real ServiceAccount; no binary is needed. The worker test
            # needs the repository's terminal backend image in the cluster.
            environment["HERMES_TEST_WORKER_IMAGE"] = import_worker_image(identifier)
            environment["HERMES_TEST_K3S_CONTAINER"] = identifier
            environment["HERMES_TEST_POSTGRES_CONTAINER"] = args.postgres_container
            failed = []
            for suite in args.suite:
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "tests.support.verification",
                        "run",
                        suite,
                        "--output",
                        f".local/verification/{suite}.json",
                    ],
                    cwd=ROOT,
                    env=environment,
                    timeout=1200,
                )
                if result.returncode:
                    failed.append(suite)
            assert not failed, f"failed: {failed}"


if __name__ == "__main__":
    main()
