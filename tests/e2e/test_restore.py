"""Restoring PostgreSQL and the storage root onto a second, empty cluster.

The scenario archives the first cluster's data, builds a second cluster, runs
`rebind-cluster` and also checks, on the restored installation, that an
instance missing from an older backup is reported as an orphan and never
adopted. Both claims are asserted inside this one case.
"""

import secrets
import subprocess
import uuid
from pathlib import Path

import pytest
import requests
import yaml

from tests.support import system
from tests.support.images import HERMES_IMAGE, MANAGER_IMAGE, WORKER_TAG
from tests.support.k3s import (
    command,
    import_image,
    remove_container,
    start_k3s,
    wait_ready,
)
from tests.support.ownership import Cleanup, database_comment

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.cluster,
    pytest.mark.destructive,
    pytest.mark.slow,
]


RESTORED = [
    ("migrate", "hermes-manager-migrate"),
    ("rebindCluster", "hermes-manager-rebind-cluster"),
]


def an_instance_missing_from_a_restored_backup_is_reported_not_adopted(
    identifier, postgres, database, ca, passwords, assignments
):
    """PostgreSQL restored from a backup older than an instance, on the same
    cluster: the instance's namespace and volume are orphans, reported and
    left alone, and their owner gets nothing."""
    admin = system.Admin(ca, passwords["alice"])
    backup = subprocess.run(
        ["docker", "exec", postgres, "pg_dump", "-U", "hermes_test", "-Fc", database],
        capture_output=True,
        timeout=300,
        check=True,
    ).stdout
    spec, _ = admin.assignment(assignments["carol"]["assignmentId"])
    command(
        [
            "docker",
            "exec",
            identifier,
            "sh",
            "-c",
            "mkdir -p /srv/hermes-data/mallory"
            " && chown 1000:1000 /srv/hermes-data/mallory",
        ]
    )
    receipt = admin.call(
        "POST",
        "/api/v1/assignments",
        {
            "identity": {
                "issuer": system.ISSUER,
                "subject": system.SUBJECTS["mallory"],
            },
            "spec": {
                **spec["spec"],
                "volumes": {"workspace": {"source": "host", "path": "mallory"}},
            },
        },
    ).json()
    namespace = admin.phase(receipt["instanceId"])["namespace"]
    system.wait(
        lambda: admin.phase(receipt["instanceId"])["phase"] == "ready",
        system.READY_TIMEOUT,
        "the instance after the backup never became ready",
        interval=5,
    )

    for name in ("hermes-manager-api", "hermes-manager-controller"):
        system.scale(identifier, system.NAMESPACE, name, 0)
    psql = ["docker", "exec", "-i", postgres, "psql", "-U", "hermes_test", "-d"]
    command([*psql, "hermes_test", "-c", f"DROP DATABASE {database} WITH (FORCE)"])
    command([*psql, "hermes_test", "-c", f"CREATE DATABASE {database}"])
    subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            postgres,
            "pg_restore",
            "-U",
            "hermes_test",
            "-d",
            database,
            "--no-owner",
        ],
        input=backup,
        capture_output=True,
        timeout=300,
        check=True,
    )
    for name in ("hermes-manager-api", "hermes-manager-controller"):
        system.scale(identifier, system.NAMESPACE, name, 1)

    volume = f"{namespace}-workspace"
    expected = {("namespace", namespace), ("persistent_volume", volume)}

    def reported():
        report = admin.call("GET", "/api/v1/orphans").json()
        return {(item["kind"], item["name"]) for item in report["items"]} == expected

    system.wait(reported, 180, "the orphaned namespace and volume were not reported")
    admin.call("GET", f"/api/v1/instances/{receipt['instanceId']}", expected=(404,))
    for kind, name in (("namespace", namespace), ("pv", volume)):
        phase = system.kubectl(
            identifier, "get", kind, name, "-o", "jsonpath={.status.phase}"
        )
        assert phase in ("Active", "Bound"), f"the orphaned {kind} changed: {phase}"
    mallory = system.Browser(ca, "mallory", passwords["mallory"])
    assert mallory.sign_in().status_code == 403, "an orphan was routed"


def restore_on_a_new_cluster_rebinds_without_adoption(
    first,
    postgres,
    network,
    database,
    temporary,
    tls,
    ca,
    passwords,
    owned,
    assignments,
    run,
    shared_domain=None,
):
    """PostgreSQL and the static storage root restored onto a new, empty
    cluster: `rebind-cluster` records it, and the controller recreates every
    instance against the restored paths with its original material."""
    admin = system.Admin(ca, passwords["alice"])
    carol = admin.phase(assignments["carol"]["instanceId"])["namespace"]
    marker = secrets.token_hex(8)
    system.instance_pod_exec(first, carol, f"echo {marker} > /workspace/e2e-restore")

    # The deployment-owned backup: a database dump and the storage roots.
    dump = subprocess.run(
        ["docker", "exec", postgres, "pg_dump", "-U", "hermes_test", "-Fc", database],
        capture_output=True,
        timeout=300,
        check=True,
    ).stdout
    archive = subprocess.run(
        ["docker", "exec", first, "tar", "-C", "/srv", "-cf", "-", "hermes-data"],
        capture_output=True,
        timeout=300,
        check=True,
    ).stdout
    with Cleanup() as cleanup:
        restored = database + "_restored"
        psql = ["docker", "exec", "-i", postgres]
        cleanup.add(
            "restored database",
            lambda: command(
                [
                    *psql,
                    "psql",
                    "-U",
                    "hermes_test",
                    "-d",
                    "hermes_test",
                    "-c",
                    f"DROP DATABASE IF EXISTS {restored} WITH (FORCE)",
                ]
            ),
        )
        for statement in (
            f"CREATE DATABASE {restored}",
            f"COMMENT ON DATABASE {restored} IS '{database_comment(run)}'",
        ):
            command(
                [
                    *psql,
                    "psql",
                    "-U",
                    "hermes_test",
                    "-d",
                    "hermes_test",
                    "-c",
                    statement,
                ]
            )
        subprocess.run(
            [*psql, "pg_restore", "-U", "hermes_test", "-d", restored, "--no-owner"],
            input=dump,
            capture_output=True,
            timeout=300,
            check=True,
        )
        address = system.container_ip(postgres, network)
        database_url = f"postgres://hermes_test@{address}:5432/{restored}"

        second = start_k3s(
            network, "personal-hermes-restore-" + uuid.uuid4().hex[:12], run
        )
        cleanup.add("second cluster", lambda: remove_container(second, run))
        config = wait_ready(second)
        kubeconfig = Path(temporary) / "kubeconfig-restore"
        kubeconfig.write_text(yaml.safe_dump(config))
        kubeconfig.chmod(0o600)
        subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                second,
                "sh",
                "-c",
                "mkdir -p /srv && tar -C /srv -xf -",
            ],
            input=archive,
            capture_output=True,
            timeout=300,
            check=True,
        )
        images = {
            "manager": import_image(second, MANAGER_IMAGE),
            "hermes": import_image(second, HERMES_IMAGE),
            "worker": import_image(second, WORKER_TAG),
        }
        keycloak_image = import_image(second, "hermes-e2e-keycloak:pinned")
        shared_hosts = (
            tuple(f"{label}.{shared_domain}" for label in system.SHARED_LABELS)
            if shared_domain
            else ()
        )
        system.keycloak(second, keycloak_image, tls, passwords, shared_hosts)
        system.kubectl(
            second,
            "rollout",
            "status",
            "--namespace",
            "keycloak",
            "deployment/keycloak",
            "--timeout=300s",
            timeout=320,
        )
        node = system.kubectl(
            second, "get", "nodes", "-o", "jsonpath={.items[0].metadata.name}"
        )
        with system.routed(system.container_ip(second, network), shared_hosts):
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
                "Keycloak did not serve the realm on the new cluster",
            )
            system.install(
                second,
                kubeconfig,
                temporary,
                images,
                database_url,
                tls,
                node,
                owned,
                RESTORED,
                shared_domain=shared_domain,
            )
            admin = system.Admin(ca, passwords["alice"])
            system.wait(
                lambda: (
                    admin.session.get(
                        f"https://{system.ADMIN}/healthz", timeout=5
                    ).status_code
                    == 200
                ),
                120,
                "the restored administration API is unreachable",
            )
            for name, receipt in assignments.items():
                system.wait(
                    lambda receipt=receipt: (
                        admin.phase(receipt["instanceId"])["phase"] == "ready"
                    ),
                    system.READY_TIMEOUT,
                    lambda receipt=receipt, name=name: (
                        f"{name}'s restored instance never became ready: "
                        f"{admin.phase(receipt['instanceId'])}"
                    ),
                    interval=5,
                )
            assert admin.phase(assignments["carol"]["instanceId"])["namespace"] == carol
            kept = system.instance_pod_exec(second, carol, "cat /workspace/e2e-restore")
            assert kept.strip() == marker, "the restored static volume lost its data"
            browser = system.Browser(ca, "carol", passwords["carol"])
            assert browser.sign_in().status_code == 200
            assert browser.native_sign_in()["user_id"] == system.SUBJECTS["carol"]
            an_instance_missing_from_a_restored_backup_is_reported_not_adopted(
                second, postgres, restored, ca, passwords, assignments
            )
    print(
        "PASS: restore onto a new cluster with rebind-cluster; instances recreated "
        "with their material; static volume data back; owner signs in again; an "
        "instance missing from an older backup reported as orphans, not adopted"
    )


def test_restore_on_a_new_cluster_rebinds_without_adoption(destructive_platform):
    platform = destructive_platform
    restore_on_a_new_cluster_rebinds_without_adoption(
        platform.identifier,
        platform.postgres,
        platform.network,
        platform.database,
        str(platform.directory),
        platform.tls,
        platform.ca,
        platform.passwords,
        platform.owned,
        platform.assignments,
        platform.cluster.run,
    )
