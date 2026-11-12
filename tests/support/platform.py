"""The installed platform on the run-owned k3d cluster, for independent cases.

A run-owned PostgreSQL on the cluster's network, the images under test (which
the caller's fixtures provide) imported into every node, Keycloak and the fake
model, the chart with its operator Jobs, and the published catalog with two
commissioned owners. Cases receive the result and run alone, in any order.

Nothing here prints or retains a credential beyond the run's temporary
directory.
"""

from __future__ import annotations

import secrets
import uuid
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

import requests

from tests.support import system
from tests.support.cluster import Cluster
from tests.support.k3s import (
    WORKER_TAG,
    command,
    import_image,
)
from tests.support.ownership import (
    FIXTURE,
    POSTGRES,
    RUN,
    Cleanup,
    remove_fixture,
)

POSTGRES_IMAGE = (
    "postgres@sha256:2a0d0fe14825b0939f78a8cad5cd4e6aa68bf94d0e5dd96e24b6d23af4315545"
)


class PlatformUnavailable(RuntimeError):
    """A prerequisite of the installed platform is missing."""


def command_status(args):
    import subprocess

    return subprocess.run(args, capture_output=True, timeout=60).returncode


def start_postgres(cluster: Cluster, cleanup: Cleanup):
    """A run-owned PostgreSQL on the cluster's network; returns its name and
    the database URL pods use."""
    name = f"hermes-pg-{cluster.run}"
    network = f"k3d-{cluster.name}"
    cleanup.add(name, lambda: remove_fixture(name, POSTGRES, cluster.run))
    command(
        [
            "docker",
            "run",
            "--detach",
            "--name",
            name,
            "--label",
            f"{FIXTURE}={POSTGRES}",
            "--label",
            f"{RUN}={cluster.run}",
            "--network",
            network,
            "--env",
            "POSTGRES_USER=hermes_test",
            "--env",
            "POSTGRES_DB=hermes_test",
            "--env",
            "POSTGRES_HOST_AUTH_METHOD=trust",
            POSTGRES_IMAGE,
        ],
        timeout=300,
    )

    def ready():
        probe = command_status(
            [
                "docker",
                "exec",
                name,
                "pg_isready",
                # The image's temporary init server answers on the unix
                # socket only; the final server is the one on TCP.
                "-h",
                "127.0.0.1",
                "-U",
                "hermes_test",
                "-d",
                "hermes_test",
            ]
        )
        return probe == 0

    cluster.wait_for(ready, 90, "PostgreSQL", interval=1)
    database = "hermes_e2e_" + uuid.uuid4().hex[:8]
    command(
        [
            "docker",
            "exec",
            name,
            "psql",
            "-U",
            "hermes_test",
            "-d",
            "hermes_test",
            "-c",
            f"CREATE DATABASE {database}",
        ]
    )
    address = system.container_ip(name, network)
    return name, database, f"postgres://hermes_test@{address}:5432/{database}"


def bring_up(cluster: Cluster, directory: Path, cleanup: Cleanup, *, shared=False):
    """The installed platform with Bob's and Carol's commissioned instances."""
    server = cluster.servers[0]
    network = f"k3d-{cluster.name}"
    postgres, database, database_url = start_postgres(cluster, cleanup)

    command(["docker", "pull", "--quiet", system.KEYCLOAK_IMAGE], timeout=900)
    command(["docker", "tag", system.KEYCLOAK_IMAGE, "hermes-e2e-keycloak:pinned"])
    references = {}
    for key, tag in (
        ("manager", system.MANAGER_IMAGE),
        ("hermes", system.HERMES_IMAGE),
        ("worker", WORKER_TAG),
        ("keycloak", "hermes-e2e-keycloak:pinned"),
    ):
        pinned = {import_image(node, tag) for node in cluster.nodes}
        if len(pinned) != 1:
            raise PlatformUnavailable(f"{tag} differs between nodes")
        references[key] = pinned.pop()
    images = {name: references[name] for name in ("manager", "hermes", "worker")}

    node_ip = system.container_ip(server, network)
    command(
        [
            "docker",
            "exec",
            server,
            "sh",
            "-c",
            "mkdir -p /srv/hermes-data/carol && chown 1000:1000 /srv/hermes-data/carol",
        ]
    )
    tls_directory = directory / "tls"
    tls_directory.mkdir()
    shared_domain = system.SHARED_DOMAIN if shared else None
    shared_hosts = (
        tuple(f"{label}.{shared_domain}" for label in system.SHARED_LABELS)
        if shared
        else ()
    )
    tls = system.certificates(tls_directory, shared_domain)
    ca = str(tls_directory / "ca.crt")
    passwords = {name: secrets.token_urlsafe(18) for name in system.SUBJECTS}
    for name in ("directory", "llm", "registry"):
        passwords[name] = secrets.token_urlsafe(24)

    system.keycloak(server, references["keycloak"], tls, passwords, shared_hosts)
    system.fake_model(server, images["hermes"])
    for namespace, name in (("keycloak", "keycloak"), ("model", "fake-model")):
        system.kubectl(
            server,
            "rollout",
            "status",
            "--namespace",
            namespace,
            f"deployment/{name}",
            "--timeout=300s",
            timeout=320,
        )
    stack = ExitStack()
    cleanup.add("name routing", stack.close)
    stack.enter_context(system.routed(node_ip, shared_hosts))
    system.wait(
        lambda: (
            requests.get(
                f"{system.ISSUER}/.well-known/openid-configuration",
                verify=ca,
                timeout=5,
            ).status_code
            == 200
        ),
        300,
        "Keycloak did not serve the realm",
    )
    owned = system.installation()
    system.install(
        server,
        cluster.kubeconfig,
        str(directory),
        images,
        database_url,
        tls,
        server,
        owned,
        system.FRESH,
        shared_domain=shared_domain,
    )
    state = system.commission(server, images, ca, passwords)
    return SimpleNamespace(
        cluster=cluster,
        identifier=server,
        postgres=postgres,
        ca=ca,
        passwords=passwords,
        images=images,
        admin=state.admin,
        assignments=state.assignments,
        state=state,
        # What a restore onto a second cluster needs.
        network=network,
        database=database,
        directory=directory,
        tls=tls,
        owned=owned,
        shared_hosts=shared_hosts,
    )
