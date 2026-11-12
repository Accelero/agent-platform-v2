"""Failure scenarios: they pause the database, scale the manager or suspend
owners, so they are `destructive` and run on the destructive platform, which
the next case gets only if this one left it healthy (`tests/support/pool.py`)."""

import secrets
import threading
import time

import pytest
import requests

from tests.support import system
from tests.support.k3s import command

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.cluster,
    pytest.mark.destructive,
    pytest.mark.slow,
    pytest.mark.security,
]


REVOKED_SCRIPT = (
    system.PLUGIN_PRELUDE
    + """
import signal, urllib.error, uuid
# Suspension stops Hermes soon after; poll fast and report every answer at
# once, so the refusal is seen before this process ends with the Pod.
signal.signal(signal.SIGTERM, signal.SIG_IGN)
settings = managed_execution.Provider(Context("")).settings()
client = managed_execution.Client(settings)
payload = {"session": "e2e-revoked", "mode": settings["mode"],
           "request": uuid.uuid4().hex}
last = None
deadline = time.monotonic() + 120
while time.monotonic() < deadline:
    try:
        client.call("/v1/leases/acquire", payload, timeout=10)
        print("granted", flush=True)
        break
    except urllib.error.HTTPError as error:
        if error.code != last:
            print(error.code, flush=True)
            last = error.code
        if error.code not in (409, 503):
            break
    time.sleep(0.1)
"""
)


def suspension_closes_open_connections_and_reenabling_keeps_data(
    identifier, admin, ca, passwords, assignments
):
    bob = system.Browser(ca, "bob", passwords["bob"])
    assert bob.sign_in().status_code == 200
    bob.native_sign_in()
    carol = system.Browser(ca, "carol", passwords["carol"])
    assert carol.sign_in().status_code == 200
    carol.native_sign_in()
    instance = assignments["bob"]["instanceId"]
    namespace = admin.phase(instance)["namespace"]
    marker = secrets.token_hex(8)
    system.instance_pod_exec(
        identifier, namespace, f"echo {marker} > /opt/data/e2e-marker"
    )
    claims = system.kubectl(
        identifier,
        "get",
        "pvc",
        "--namespace",
        namespace,
        "-o",
        "jsonpath={.items[*].metadata.uid}",
    )

    # Suspended during an open chat connection and during provisioning.
    connection = bob.websocket()
    known = {e["id"] for e in system.environments(admin, instance)}
    provisioning = system.hermes_python_started(identifier, namespace, REVOKED_SCRIPT)
    pending = system.wait(
        lambda: [
            e
            for e in system.environments(admin, instance)
            if e["id"] not in known and e["session"] == "e2e-revoked"
        ],
        120,
        "the acquisition created no environment",
        interval=0.3,
    )
    admin.set_access(assignments["bob"]["assignmentId"], "suspended")
    took = system.closed_within(connection, 15)
    assert took < 12, f"open WebSocket outlived suspension by {took:.1f}s"
    output, _ = provisioning.communicate(timeout=180)
    answers = output.split()
    assert "granted" not in answers, "a lease was granted"
    assert answers[:1] == ["409"] and answers[-1] == "401", answers
    time.sleep(6)
    assert bob.get("/api/auth/me").status_code == 403
    assert carol.get("/api/auth/me").status_code == 200
    system.wait(
        lambda: admin.phase(instance)["phase"] == "retained",
        300,
        "bob's instance was not retained",
        interval=5,
    )
    drained = {e["id"]: e for e in system.environments(admin, instance)}[
        pending[0]["id"]
    ]
    assert (drained["state"], drained["leases"]) == ("retired", 0), drained
    workers = system.kubectl(
        identifier, "get", "pods", "--namespace", namespace, "-o", "name"
    ).split()
    assert not workers, f"Pods left in a retained instance: {workers}"

    admin.set_access(assignments["bob"]["assignmentId"], "enabled")
    statuses = set()

    def back():
        reply = bob.get("/api/auth/me")
        statuses.add(reply.status_code)
        return reply.status_code == 200

    system.wait(
        back, system.READY_TIMEOUT, "bob's instance did not come back", interval=5
    )
    assert 202 in statuses, f"no owner status page while starting: {statuses}"
    assert admin.phase(instance)["id"] == instance
    kept = system.instance_pod_exec(identifier, namespace, "cat /opt/data/e2e-marker")
    assert kept.strip() == marker, "the home volume was replaced"
    assert (
        system.kubectl(
            identifier,
            "get",
            "pvc",
            "--namespace",
            namespace,
            "-o",
            "jsonpath={.items[*].metadata.uid}",
        )
        == claims
    )
    signed_out = bob.post("/auth/logout", allow_redirects=False)
    assert signed_out.status_code == 303
    assert signed_out.headers["Location"] == "/_platform/signed-out"
    again = bob.get("/", headers={"Accept": "text/html"}, allow_redirects=False)
    assert again.status_code == 303
    assert again.headers["Location"].startswith("/_platform/login")


def closing(connection):
    """Watches `connection` in the background; the list receives the time the
    server closed it."""
    closed = []

    def watch():
        system.closed_within(connection, 900)
        closed.append(time.monotonic())

    threading.Thread(target=watch, daemon=True).start()
    return closed


def denied_within(bob, connection_closed, started, requests_bound, close_bound):
    """New requests fail closed within `requests_bound` seconds of `started`
    and the open WebSocket closes within `close_bound`."""
    system.wait(
        lambda: bob.get("/api/auth/me", allow_redirects=False).status_code != 200,
        requests_bound + 10,
        "requests still reached the instance",
        interval=0.5,
    )
    took = time.monotonic() - started
    assert took <= requests_bound, f"requests were denied only after {took:.1f}s"
    system.wait(
        lambda: connection_closed, close_bound + 10, "the WebSocket stayed open", 0.5
    )
    took = connection_closed[0] - started
    assert took <= close_bound, f"the WebSocket closed only after {took:.1f}s"


def outages_and_provider_revocation_fail_closed_within_the_bounds(
    identifier, postgres, admin, ca, passwords, assignments
):
    """No manager, no database, a person disabled at the provider, no
    provider: each denies within the bounds and nothing falls back."""
    # The manager's API, which resolves routes.
    bob = system.signed_in(ca, "bob", passwords)
    closed = closing(bob.websocket())
    system.scale(identifier, system.NAMESPACE, "hermes-manager-api", 0)
    denied_within(bob, closed, time.monotonic(), 8, 12)
    system.scale(identifier, system.NAMESPACE, "hermes-manager-api", 1)
    system.wait(lambda: bob.get("/api/auth/me").status_code == 200, 120, "no recovery")

    # The database.
    closed = closing(bob.websocket())
    command(["docker", "pause", postgres])
    try:
        denied_within(bob, closed, time.monotonic(), 8, 12)
    finally:
        command(["docker", "unpause", postgres])
    system.wait(lambda: bob.get("/api/auth/me").status_code == 200, 120, "no recovery")

    # The provider disables Bob: his session ends within 130 s.
    directory = system.Directory(ca, passwords["directory"])
    closed = closing(bob.websocket())
    directory.update(system.SUBJECTS["bob"], enabled=False)
    denied_within(bob, closed, time.monotonic(), 130, 130)
    directory.update(system.SUBJECTS["bob"], enabled=True)

    # No provider: evidence older than 120 s is refused, and nobody signs in.
    bob = system.signed_in(ca, "bob", passwords)
    closed = closing(bob.websocket())
    system.scale(identifier, "keycloak", "keycloak", 0)
    denied_within(bob, closed, time.monotonic(), 130, 130)
    page = bob.get("/", headers={"Accept": "text/html"}, allow_redirects=False)
    assert page.status_code != 200, "a page was served without a provider"
    system.scale(identifier, "keycloak", "keycloak", 1)
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
        "Keycloak did not come back",
    )
    # The restarted provider imported the realm again; everyone signs in.
    assert system.signed_in(ca, "bob", passwords).get("/api/auth/me").status_code == 200
    assert admin.call("GET", "/api/v1/session").status_code == 200


def test_outages_and_provider_revocation_fail_closed_within_the_bounds(
    destructive_platform,
):
    platform = destructive_platform
    outages_and_provider_revocation_fail_closed_within_the_bounds(
        platform.identifier,
        platform.postgres,
        platform.admin,
        platform.ca,
        platform.passwords,
        platform.assignments,
    )


def test_suspension_closes_open_connections_and_reenabling_keeps_data(
    destructive_platform,
):
    platform = destructive_platform
    suspension_closes_open_connections_and_reenabling_keeps_data(
        platform.identifier,
        platform.admin,
        platform.ca,
        platform.passwords,
        platform.assignments,
    )
