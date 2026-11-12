"""New people and instances for cases that must not touch the commissioned ones.

A case makes its own directory person with an assignment, waits for the
instance and works in it. Nothing here prints a credential.
"""

from __future__ import annotations

import secrets

from tests.support import system


def template(platform, **extra):
    """A published-template definition shaped like the commissioned `personal`
    one (a workspace volume and mount), with `extra` fields added."""
    return {
        "schemaVersion": 2,
        "image": platform.images["hermes"],
        "resources": system.HERMES_RESOURCES,
        "home": {"source": "local", "sizeGib": 1},
        "mounts": {"workspace": {"mountPath": "/workspace"}},
        "defaultVolumes": {"workspace": {"source": "local", "sizeGib": 1}},
        **extra,
    }


def person(platform, prefix, spec):
    """A new directory person with the assignment `spec`; returns the receipt."""
    username = f"{prefix}-{secrets.token_hex(3)}"
    directory = system.Directory(platform.ca, platform.passwords["directory"])
    subject = directory.create(username, f"{username}@e2e.test", secrets.token_hex(12))
    return platform.admin.call(
        "POST",
        "/api/v1/assignments",
        {
            "identity": {"issuer": system.ISSUER, "subject": subject},
            "spec": {"access": "enabled", **spec},
        },
    ).json()


def current(instance):
    """Ready at the desired state: right after a change the phase still reads
    `ready` from before it, while the Pod that is about to be replaced runs."""
    return (
        instance["phase"] == "ready"
        and instance["observedGeneration"] == instance["desiredGeneration"]
    )


def ready(platform, receipt):
    """Waits for the instance to be ready at its desired state; returns its
    namespace."""
    system.wait(
        lambda: current(platform.admin.phase(receipt["instanceId"])),
        system.READY_TIMEOUT,
        lambda: platform.admin.phase(receipt["instanceId"]),
        interval=5,
    )
    return platform.admin.phase(receipt["instanceId"])["namespace"]
