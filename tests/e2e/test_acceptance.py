"""Acceptance scenarios of the installed platform, each an independent case.

The assertions are those of the standalone end-to-end program's scenario
helpers, kept as they are; what changes is that each scenario is collected,
reported and runnable on its own against the same installed platform, in any
order (set `HERMES_TEST_ORDER=reverse` to run them back to front).
"""

import base64
import hashlib
import json
import secrets
import ssl
import subprocess
import tempfile
import time
import uuid

import pytest
import requests
import websocket
import yaml

from tests.support import system
from tests.support.k3s import command, import_fixed_worker_image, import_worker_image

pytestmark = [pytest.mark.e2e, pytest.mark.cluster, pytest.mark.slow]


def model_requests(identifier):
    """What the fake model saw: `{path, key, model}` per request, keys
    hashed."""
    output = system.kubectl(
        identifier,
        "exec",
        "--namespace",
        "model",
        "deployment/fake-model",
        "--",
        "/opt/hermes/.venv/bin/python",
        "-c",
        "import sys, urllib.request; "
        "sys.stdout.write(urllib.request.urlopen('http://127.0.0.1:8080/seen').read().decode())",
    )
    return json.loads(output)["requests"]


def a_missing_static_directory_blocks_without_a_substitute(
    identifier, admin, template, terminal
):
    """A static volume whose directory does not exist blocks the instance with
    a storage condition; nothing creates the directory, and the instance
    starts once it exists."""
    receipt = admin.call(
        "POST",
        "/api/v1/assignments",
        {
            "identity": {"issuer": system.ISSUER, "subject": system.SUBJECTS["erin"]},
            "spec": {
                "access": "enabled",
                "template": {"id": "personal", "version": template},
                "volumes": {"workspace": {"source": "host", "path": "erin"}},
                "terminal": terminal,
            },
        },
    ).json()

    def blocked():
        view = admin.phase(receipt["instanceId"])
        storage = next((c for c in view["conditions"] if c["type"] == "storage"), None)
        return (
            view["phase"] == "blocked"
            and view["blockedReason"] == "storage_unavailable"
            and storage
            == {"type": "storage", "status": "false", "reason": "mount_failed"}
        )

    system.wait(
        blocked,
        300,
        lambda: f"erin's instance did not block: {admin.phase(receipt['instanceId'])}",
        interval=5,
    )
    missing = subprocess.run(
        ["docker", "exec", identifier, "test", "-e", "/srv/hermes-data/erin"],
        capture_output=True,
        timeout=30,
    )
    assert missing.returncode != 0, "a directory was created in place of the volume"
    command(
        [
            "docker",
            "exec",
            identifier,
            "sh",
            "-c",
            "mkdir /srv/hermes-data/erin && chown 1000:1000 /srv/hermes-data/erin",
        ]
    )
    system.wait(
        lambda: admin.phase(receipt["instanceId"])["phase"] == "ready",
        system.READY_TIMEOUT,
        lambda: (
            f"erin's instance did not recover: {admin.phase(receipt['instanceId'])}"
        ),
        interval=5,
    )
    admin.release(receipt)


TERMINAL_SCRIPT = (
    system.PLUGIN_PRELUDE
    + """
result = {}
result["available"] = managed_execution.Provider(Context("")).is_available()
shell = environment("", "e2e-cron")
reply = shell.execute(
    "id -u; id -g; pwd; printf terminal-ok > /workspace/e2e-terminal")
result["output"], result["returncode"] = reply["output"], reply["returncode"]
shell.cleanup()
denied = refused("not-allowed", "e2e-denied")
result["denied"] = bool(denied) and (
    "rejected" in denied.lower() or "not" in denied.lower())
print(json.dumps(result))
"""
)


def a_hermes_without_browser_traffic_acquires_a_terminal_backend(
    identifier, admin, assignments
):
    """The plugin in a running Hermes, with no browser involved, acquires the
    assignment's default backend, runs as the data identity in the shared
    workspace, and is refused a backend outside the assignment."""
    namespace = admin.phase(assignments["bob"]["instanceId"])["namespace"]
    output = system.kubectl(
        identifier,
        "exec",
        "-i",
        "--namespace",
        namespace,
        "deployment/hermes",
        "-c",
        "hermes",
        "--",
        "/opt/hermes/.venv/bin/python",
        "-",
        data=TERMINAL_SCRIPT.encode(),
        timeout=400,
    )
    result = json.loads(output.strip().splitlines()[-1])
    assert result["available"], "the plugin found no managed configuration"
    assert result["returncode"] == 0, result
    lines = result["output"].split()
    assert lines[:3] == ["1000", "1000", "/workspace"], result
    assert result["denied"], "a backend outside the assignment was served"
    shared = system.instance_pod_exec(
        identifier, namespace, "cat /workspace/e2e-terminal"
    )
    assert shared == "terminal-ok", "the terminal backend wrote somewhere else"


def two_owners_reach_their_own_native_instances_through_the_gateway(
    identifier, admin, ca, passwords, assignments
):
    for name in ("bob", "carol"):
        browser = system.Browser(ca, name, passwords[name])
        landed = browser.sign_in()
        assert landed.status_code == 200, f"{name}: {landed.status_code}"
        me = browser.get("/_platform/me").json()
        assert me["identity"] == {
            "issuer": system.ISSUER,
            "subject": system.SUBJECTS[name],
        }
        native = browser.native_sign_in()
        assert native["user_id"] == system.SUBJECTS[name], (
            f"{name} reached another owner"
        )
    # Signing in enrolls nobody: no assignment and no namespace appear.
    before = system.namespaces(identifier)
    assigned = admin.call("GET", "/api/v1/assignments?limit=50").json()["items"]
    mallory = system.Browser(ca, "mallory", passwords["mallory"])
    denied = mallory.sign_in()
    assert denied.status_code == 403, f"mallory: {denied.status_code}"
    assert "No Hermes for this account" in denied.text
    time.sleep(5)
    assert system.namespaces(identifier) == before, "a login created a namespace"
    assert admin.call("GET", "/api/v1/assignments?limit=50").json()["items"] == assigned
    # Each instance has its own credentials and keys.
    held = {
        name: system.secret_digests(
            identifier,
            admin.phase(receipt["instanceId"])["namespace"],
            "hermes-credentials",
        )
        for name, receipt in assignments.items()
    }
    shared = set(held["bob"].values()) & set(held["carol"].values())
    assert held["bob"] and not shared, "two instances share credential material"


def owners_share_a_static_folder(identifier, admin, assignments, template, terminal):
    """Two real instances and a worker share Carol's existing local folder."""
    receipt = admin.call(
        "POST",
        "/api/v1/assignments",
        {
            "identity": {"issuer": system.ISSUER, "subject": str(uuid.uuid4())},
            "spec": {
                "access": "enabled",
                "template": {"id": "personal", "version": template},
                "volumes": {
                    "workspace": {"source": "host", "path": "carol", "readOnly": True}
                },
                "terminal": terminal,
            },
        },
    ).json()
    system.wait(
        lambda: admin.phase(receipt["instanceId"])["phase"] == "ready",
        system.READY_TIMEOUT,
        "shared read-only instance did not become ready",
        interval=5,
    )
    events = admin.call(
        "GET",
        f"/api/v1/audit-events?targetKind=assignment&targetId={receipt['assignmentId']}",
    ).json()["items"]
    assert events[0]["change"]["volumes"]["workspace"] == {
        "source": "host",
        "path": "carol",
        "readOnly": True,
    }
    uses = admin.call("GET", "/api/v1/volumes?source=host").json()["items"]
    shared_uses = [use for use in uses if use.get("path") == "carol"]
    assert {(use["assignmentId"], use["readOnly"]) for use in shared_uses} == {
        (assignments["carol"]["assignmentId"], False),
        (receipt["assignmentId"], True),
    }
    writable = admin.phase(assignments["carol"]["instanceId"])["namespace"]
    readonly = admin.phase(receipt["instanceId"])["namespace"]
    marker = secrets.token_hex(8)
    system.instance_pod_exec(
        identifier, writable, f"printf {marker} > /workspace/e2e-drive"
    )
    assert (
        system.instance_pod_exec(identifier, readonly, "cat /workspace/e2e-drive")
        == marker
    )
    # Invert the shell result so kubectl succeeds only when the write fails.
    denied = system.instance_pod_exec(
        identifier,
        readonly,
        "if touch /workspace/e2e-forbidden 2>/tmp/e2e-write-error; "
        "then exit 1; fi; cat /tmp/e2e-write-error",
    )
    assert "Read-only file system" in denied, denied
    script = (
        system.PLUGIN_PRELUDE
        + """
worker = environment("", "e2e-shared-drive")
result = worker.execute("cat /workspace/e2e-drive; "
                        "printf worker > /workspace/e2e-worker-drive")
worker.cleanup()
print(json.dumps(result))
"""
    )
    result = system.hermes_python(identifier, writable, script)
    assert result["returncode"] == 0 and result["output"].strip() == marker, result
    assert (
        system.instance_pod_exec(
            identifier, readonly, "cat /workspace/e2e-worker-drive"
        )
        == "worker"
    )
    readonly_script = (
        system.PLUGIN_PRELUDE
        + """
worker = environment("", "e2e-readonly-drive")
result = worker.execute(
    "cat /workspace/e2e-worker-drive; "
    "if touch /workspace/e2e-worker-forbidden 2>/tmp/write-error; "
    "then exit 1; fi; grep -q 'Read-only file system' /tmp/write-error")
worker.cleanup()
print(json.dumps(result))
"""
    )
    result = system.hermes_python(identifier, readonly, readonly_script)
    assert result["returncode"] == 0 and result["output"].strip() == "worker", result
    pvs = json.loads(system.kubectl(identifier, "get", "pv", "-o", "json"))["items"]
    shared = [
        pv
        for pv in pvs
        if pv["spec"].get("local", {}).get("path") == "/srv/hermes-data/carol"
    ]
    assert len(shared) == 2 and len({pv["metadata"]["name"] for pv in shared}) == 2
    assert {pv["spec"]["claimRef"]["namespace"] for pv in shared} == {
        writable,
        readonly,
    }
    assert all(pv["spec"]["accessModes"] == ["ReadWriteOnce"] for pv in shared)
    admin.release(receipt)


def chat_settings_and_the_admin_secret_reach_only_the_referencing_instance(
    identifier, admin, ca, passwords, assignments
):
    """Bob's settings layer under the template's and assignment's settings and
    the platform's; the model key reaches only his instance, which the fake
    model sees, and no API returns it."""
    bob = system.signed_in(ca, "bob", passwords)
    saved = bob.put(
        "/api/config",
        json={
            "config": {
                # The web form's string form of `model.default`.
                "model": "user-model",
                "display": {"compact": False, "timestamps": True},
                "terminal": {"backend": "local"},
            }
        },
    )
    assert saved.status_code == 200, f"saving settings: {saved.status_code}"
    effective = bob.get("/api/config").json()
    assert effective["model"] == "fake-model", "the template's model lost"
    assert effective["display"]["compact"] is True, "the assignment lost"
    assert effective["terminal"]["backend"] == "managed_execution"
    assert effective["display"]["timestamps"] is True, "the owner's setting is gone"
    assert passwords["llm"] not in json.dumps(effective)
    bob_namespace = admin.phase(assignments["bob"]["instanceId"])["namespace"]
    # The owner's file keeps their own setting; managed values are never
    # written into it, and Hermes drops writes to managed keys.
    own = system.instance_pod_exec(
        identifier, bob_namespace, "cat /opt/data/config.yaml"
    )
    assert yaml.safe_load(own)["display"] == {"timestamps": True}, "setting lost"
    for managed in ("fake-model", system.MODEL_URL, "api_key", "compact"):
        assert managed not in own, "a managed value was written to the owner's file"

    key = hashlib.sha256(passwords["llm"].encode()).hexdigest()
    seen = len(model_requests(identifier))
    session, text = bob.chat("hello")
    assert text == "hello from e2e", f"unexpected reply: {text[:200]}"
    requests_made = model_requests(identifier)[seen:]
    assert all(r["key"] == key for r in requests_made), "the model saw another key"
    models = {
        r["model"] for r in requests_made if r["path"].endswith("/chat/completions")
    }
    assert models == {"fake-model"}, f"the model was asked for {models}"
    history = bob.get("/api/sessions")
    assert history.status_code == 200, f"history: {history.status_code}"
    stored = session.get("stored_session_id") or session["session_id"]
    assert stored in history.text, "the conversation is not in Bob's history"

    carol = system.signed_in(ca, "carol", passwords)
    theirs = carol.get("/api/sessions")
    assert theirs.status_code == 200 and stored not in theirs.text
    carol_namespace = admin.phase(assignments["carol"]["instanceId"])["namespace"]
    delivered = system.instance_pod_exec(
        identifier, bob_namespace, 'printf %s "$OPENAI_API_KEY" | sha256sum'
    )
    assert delivered.split()[0] == key, "Bob's instance lacks the model key"
    missing = subprocess.run(
        [
            "docker",
            "exec",
            identifier,
            "kubectl",
            "exec",
            "--namespace",
            carol_namespace,
            "deployment/hermes",
            "-c",
            "hermes",
            "--",
            "printenv",
            "OPENAI_API_KEY",
        ],
        capture_output=True,
        timeout=60,
    )
    assert missing.returncode != 0, "Carol's instance received the model key"
    encoded = base64.b64encode(passwords["llm"].encode()).decode()
    for namespace, present in ((bob_namespace, True), (carol_namespace, False)):
        listing = system.kubectl(
            identifier,
            "get",
            "secrets",
            "--namespace",
            namespace,
            "-o",
            "json",
            quiet=True,
        )
        assert (encoded in listing) is present, (
            f"model key in {namespace}: {not present}"
        )
    for path in ("/api/v1/secrets", "/api/v1/audit-events?limit=50"):
        assert passwords["llm"] not in admin.call("GET", path).text, path
        assert encoded not in admin.call("GET", path).text, path


def uploads_reach_only_the_owners_private_home(
    identifier, admin, ca, passwords, assignments
):
    """Native file management, rooted in the private Hermes home: Bob's upload
    lands in his home only, and the gateway bounds its size."""
    bob = system.signed_in(ca, "bob", passwords)
    carol = system.signed_in(ca, "carol", passwords)
    marker = secrets.token_hex(8)
    uploaded = bob.upload("uploads/e2e.txt", marker.encode())
    assert uploaded.status_code == 200, f"upload: {uploaded.status_code}"
    listing = bob.get("/api/files", params={"path": "uploads"})
    assert listing.status_code == 200 and "e2e.txt" in listing.text
    theirs = carol.get("/api/files", params={"path": "uploads"})
    assert "e2e.txt" not in theirs.text, "Carol sees Bob's upload"
    bob_namespace = admin.phase(assignments["bob"]["instanceId"])["namespace"]
    carol_namespace = admin.phase(assignments["carol"]["instanceId"])["namespace"]
    stored = system.instance_pod_exec(
        identifier, bob_namespace, "cat /opt/data/uploads/e2e.txt"
    )
    assert stored == marker, "the upload did not reach Bob's home"
    elsewhere = system.instance_pod_exec(
        identifier, carol_namespace, "test -e /opt/data/uploads/e2e.txt; echo $?"
    )
    assert elsewhere.strip() == "1", "the upload reached Carol's instance"
    # The gateway bounds bodies at 64 MiB before an instance sees them.
    oversized = bob.post(
        "/api/files/upload",
        data=b"x" * (64 * 1024 * 1024 + 1),
        headers={"Content-Type": "application/json"},
        timeout=120,
    )
    assert oversized.status_code == 413, f"oversized upload: {oversized.status_code}"


def a_cron_job_fires_without_browser_traffic_and_acquires_a_terminal(
    identifier, admin, ca, passwords, assignments
):
    """A recurring job saved by Bob fires on its own, with no browser
    connected, and its terminal command runs in his backend."""
    instance = assignments["bob"]["instanceId"]
    namespace = admin.phase(instance)["namespace"]
    known = {e["id"] for e in system.environments(admin, instance)}
    marker = secrets.token_hex(8)
    bob = system.signed_in(ca, "bob", passwords)
    job = bob.post(
        "/api/cron/jobs",
        json={
            "name": "e2e",
            "schedule": "every 1m",
            "prompt": f"run: printf cron-{marker} > /workspace/e2e-cron",
            "deliver": "local",
        },
    )
    assert job.status_code == 200, f"cron job: {job.status_code} {job.text[:200]}"
    job_id = job.json()["id"]
    bob.session.close()
    fired = system.wait(
        lambda: (
            system.instance_pod_exec(
                identifier, namespace, "cat /workspace/e2e-cron 2>/dev/null || true"
            )
            == f"cron-{marker}"
        ),
        420,
        "the cron job never ran its command",
        interval=10,
    )
    assert fired
    acquired = [e for e in system.environments(admin, instance) if e["id"] not in known]
    assert acquired, "the cron run acquired no terminal environment"
    ready = system.kubectl(
        identifier,
        "get",
        "deployment",
        "hermes",
        "--namespace",
        namespace,
        "-o",
        "jsonpath={.spec.replicas} {.status.readyReplicas}",
    )
    assert ready.split() == ["1", "1"], f"Hermes replicas: {ready}"
    bob = system.signed_in(ca, "bob", passwords)
    removed = bob.session.delete(
        f"https://{system.PUBLIC}/api/cron/jobs/{job_id}",
        headers={"Origin": f"https://{system.PUBLIC}"},
        timeout=30,
    )
    assert removed.status_code == 200, f"removing the job: {removed.status_code}"


TOOLS_SCRIPT = (
    system.PLUGIN_PRELUDE
    + """
result = {}
# The private Hermes home, where uploads land, is never in a worker.
tools = environment("tools", "e2e-tools")
reply = tools.execute(
    "id -u; ls -d /home/worker/home && printf tools-ok > /home/worker/home/e2e-tools;"
    " cat /workspace/e2e-shared; echo; test -e /opt/data/uploads/e2e.txt; echo $?")
result["tools"] = [reply["output"], reply["returncode"]]
result["tools_environment"] = tools.managed_environment
# Released but not closed: the environment idles until the backend retires it.
tools._release_lease()
shell = environment("", "e2e-shell")
reply = shell.execute(
    "test -e /home/worker/home; echo $?; cat /workspace/e2e-shared; echo;"
    " test -e /opt/data/uploads/e2e.txt; echo $?")
result["shell"] = [reply["output"], reply["returncode"]]
shell.cleanup()
print(json.dumps(result))
"""
)


def bobs_backends_share_the_workspace_and_idle_workers_retire(
    identifier, admin, assignments
):
    """Bob's example: the workspace holds the same files in Hermes and both
    backends; his network home is mounted only in `tools`, his private Hermes
    home in neither. An idle `tools` worker retires without touching Hermes."""
    instance = assignments["bob"]["instanceId"]
    namespace = admin.phase(instance)["namespace"]
    pod = system.hermes_pod(identifier, namespace)
    shared = secrets.token_hex(8)
    system.instance_pod_exec(
        identifier, namespace, f"printf {shared} > /workspace/e2e-shared"
    )
    result = system.hermes_python(identifier, namespace, TOOLS_SCRIPT)
    output, code = result["tools"]
    expected = ["1000", "/home/worker/home", shared, "1"]
    assert code == 0 and output.split() == expected, output
    output, code = result["shell"]
    assert code == 0 and output.split() == ["1", shared, "1"], output
    written = command(
        ["docker", "exec", identifier, "cat", "/srv/hermes-data/homes/bob/e2e-tools"]
    )
    assert written == "tools-ok", "the network home is not Bob's host path"
    claims = system.kubectl(
        identifier,
        "get",
        "deployment",
        "hermes",
        "--namespace",
        namespace,
        "-o",
        "jsonpath={.spec.template.spec.volumes[*].persistentVolumeClaim.claimName}",
    ).split()
    assert "vol-home" not in claims, f"Hermes mounts the backend-only home: {claims}"

    environment = result["tools_environment"]

    def retired():
        states = {e["id"]: e["state"] for e in system.environments(admin, instance)}
        return states.get(environment) == "retired"

    system.wait(retired, 300, "the idle tools worker never retired", interval=5)
    assert system.hermes_pod(identifier, namespace) == pod, (
        "an idle worker restarted Hermes"
    )
    kept = system.instance_pod_exec(identifier, namespace, "cat /workspace/e2e-shared")
    assert kept == shared, "the workspace changed"


def a_backend_identity_controls_its_workers_and_keeps_the_hermes_home_private(
    identifier, admin, template, worker
):
    """A backend-only folder belongs to its worker identity. The default image
    takes any identity and a specialized image bakes one; a custom image that
    ignores the identity it runs as fails."""
    with tempfile.TemporaryDirectory(prefix="hermes-other-identity-") as temporary:
        baked = import_worker_image(identifier, uid=2000, gid=3000)
        fixed = import_fixed_worker_image(identifier, temporary)
    folder = "other-user-" + uuid.uuid4().hex[:8]
    command(
        [
            "docker",
            "exec",
            identifier,
            "sh",
            "-c",
            f"mkdir /srv/hermes-data/{folder} && "
            f"chown 2000:3000 /srv/hermes-data/{folder} && "
            f"chmod 700 /srv/hermes-data/{folder}",
        ]
    )
    definition = {
        **worker,
        "uid": 2000,
        "gid": 3000,
        "limits": {"idleSeconds": 60, "maxLeaseSeconds": 900, "maxEnvironments": 2},
        "mounts": {"drive": {"mountPath": "/drive"}},
    }
    baked_id, bad_id = folder + "-baked", folder + "-bad"
    # The default image (in `worker`) is not built for 2000:3000.
    served = {
        folder: admin.publish("terminal-backends", folder, definition),
        baked_id: admin.publish(
            "terminal-backends", baked_id, {**definition, "image": baked}
        ),
    }
    bad = admin.publish("terminal-backends", bad_id, {**definition, "image": fixed})
    receipt = admin.call(
        "POST",
        "/api/v1/assignments",
        {
            "identity": {"issuer": system.ISSUER, "subject": str(uuid.uuid4())},
            "spec": {
                "access": "enabled",
                "template": {"id": "personal", "version": template},
                "volumes": {"drive": {"source": "host", "path": folder}},
                "terminal": {
                    "default": folder,
                    "allowed": {**served, bad_id: bad},
                },
            },
        },
    ).json()
    instance = receipt["instanceId"]
    system.wait(
        lambda: admin.phase(instance)["phase"] == "ready",
        system.READY_TIMEOUT,
        "the alternate-identity assignment did not become ready",
        interval=5,
    )
    namespace = admin.phase(instance)["namespace"]
    script = (
        system.PLUGIN_PRELUDE
        + """
provider = managed_execution.Provider(Context("BACKEND"))
worker = provider.create_environment(
    cwd="/drive", timeout=120, task_id="other-identity")
result = worker.execute("rm -f /drive/created; id -u; id -g; id -un; "
                        "printf owned > /drive/created; test ! -e /opt/data")
worker.cleanup()
print(json.dumps(result))
"""
    )
    for backend in served:
        result = system.hermes_python(
            identifier, namespace, script.replace("BACKEND", backend)
        )
        assert result["returncode"] == 0, (backend, result)
        # The account is `worker` at the declared identity, whichever image.
        assert result["output"].split() == ["2000", "3000", "worker"], (backend, result)
        ownership = command(
            [
                "docker",
                "exec",
                identifier,
                "stat",
                "-c",
                "%u:%g",
                f"/srv/hermes-data/{folder}/created",
            ]
        )
        assert ownership.strip() == "2000:3000", (backend, ownership)
    home = system.instance_pod_exec(
        identifier,
        namespace,
        "id -u; id -g; stat -c '%g' /opt/data; test -w /opt/data; test ! -e /drive",
    )
    # `fsGroup` gives the home root the data GID; its owner is whatever the
    # storage source created (root for local-path), so only the group is pinned.
    assert home.split() == ["1000", "1000", "1000"], home
    failure_script = (
        system.PLUGIN_PRELUDE
        + """
try:
    provider = managed_execution.Provider(Context("BACKEND"))
    worker = provider.create_environment(
        cwd="/drive", timeout=120, task_id="bad-identity")
    worker.execute("true")
except Exception:
    print(json.dumps({"refused": True}))
else:
    worker.cleanup()
    print(json.dumps({"refused": False}))
"""
    )
    result = system.hermes_python(
        identifier, namespace, failure_script.replace("BACKEND", bad_id)
    )
    assert result["refused"], "an image without the declared worker identity ran"
    failures = [
        e["failure"]
        for e in system.environments(admin, instance)
        if e["backend"]["id"] == bad_id
    ]
    assert failures == ["contract_failed"], failures
    admin.release(receipt)


def cookie_header(browser, native):
    """`browser`'s platform cookies, or its native ones."""
    return "; ".join(
        f"{cookie.name}={cookie.value}"
        for cookie in browser.session.cookies
        if cookie.domain.lstrip(".") == system.PUBLIC
        and ("hermes-platform" in cookie.name.lower()) != native
    )


def platform_routing_ignores_native_identity_and_refuses_foreign_tickets(ca, passwords):
    """Platform identity chooses the instance even with another native identity.
    Native cookies are realm credentials; foreign tickets and header claims fail.
    """
    bob = system.signed_in(ca, "bob", passwords)
    carol = system.signed_in(ca, "carol", passwords)
    raw = requests.Session()
    raw.verify = ca
    foreign = raw.get(
        f"https://{system.PUBLIC}/api/auth/me",
        headers={
            "Cookie": cookie_header(bob, False) + "; " + cookie_header(carol, True)
        },
        timeout=30,
    )
    assert (
        foreign.status_code == 200
        and foreign.json()["user_id"] == system.SUBJECTS["carol"]
    )
    # A turn of Bob's own, so the two histories differ whatever ran before.
    # A history is compared by its sessions and their owners: a new session's
    # title is written after its turn, so other fields move between reads.
    bob.chat("routing")

    def sessions(reply):
        return [(item["id"], item["user_id"]) for item in reply.json()["sessions"]]

    bob_history = sessions(bob.get("/api/sessions"))
    carol_history = sessions(carol.get("/api/sessions"))
    assert bob_history and bob_history != carol_history
    mixed = raw.get(
        f"https://{system.PUBLIC}/api/sessions",
        headers={
            "Cookie": cookie_header(bob, False) + "; " + cookie_header(carol, True)
        },
        timeout=30,
    )
    assert mixed.status_code == 200
    assert sessions(mixed) == bob_history
    alone = raw.get(
        f"https://{system.PUBLIC}/api/auth/me",
        headers={"Cookie": cookie_header(carol, True), "Accept": "text/html"},
        allow_redirects=False,
        timeout=30,
    )
    assert alone.status_code in (303, 401), f"no platform session: {alone.status_code}"

    ticket = carol.post("/api/auth/ws-ticket").json()["ticket"]
    try:
        connection = websocket.create_connection(
            f"wss://{system.PUBLIC}/api/events?channel=e2e&ticket={ticket}",
            header=[f"Cookie: {bob.cookie_header()}"],
            origin=f"https://{system.PUBLIC}",
            sslopt={"ca_certs": ca, "cert_reqs": ssl.CERT_REQUIRED},
            timeout=20,
        )
    except websocket.WebSocketException:
        pass
    else:
        assert system.closed_within(connection, 10) < 10, (
            "a foreign ticket was accepted"
        )

    forged = bob.get(
        "/api/auth/me",
        headers={
            "X-Forwarded-User": system.SUBJECTS["carol"],
            "X-Forwarded-Email": "carol@e2e.test",
            "X-Hermes-Owner": system.SUBJECTS["carol"],
            "X-Auth-Request-User": system.SUBJECTS["carol"],
            "Remote-User": system.SUBJECTS["carol"],
        },
    )
    assert (
        forged.status_code == 200 and forged.json()["user_id"] == system.SUBJECTS["bob"]
    )


def an_email_change_keeps_ownership_and_the_old_email_gets_nothing(
    ca, passwords, assignments
):
    directory = system.Directory(ca, passwords["directory"])
    directory.update(system.SUBJECTS["bob"], email="robert@e2e.test")
    bob = system.signed_in(ca, "bob", passwords)
    me = bob.get("/_platform/me").json()
    assert me["identity"] == {
        "issuer": system.ISSUER,
        "subject": system.SUBJECTS["bob"],
    }
    assert bob.get("/api/auth/me").json()["user_id"] == system.SUBJECTS["bob"]
    password = secrets.token_urlsafe(18)
    subject = directory.create("bob-new", "bob@e2e.test", password)
    assert subject != system.SUBJECTS["bob"]
    newcomer = system.Browser(ca, "bob-new", password)
    denied = newcomer.sign_in()
    assert denied.status_code == 403, f"the old email reached: {denied.status_code}"
    assert "No Hermes for this account" in denied.text


def only_the_controller_holds_kubernetes_credentials_and_logs_hold_no_secrets(
    identifier, admin, ca, passwords, assignments
):
    controller = f"system:serviceaccount:{system.NAMESPACE}:hermes-manager-controller"
    bob_namespace = admin.phase(assignments["bob"]["instanceId"])["namespace"]

    def can(user, *request):
        return (
            system.kubectl(
                identifier, "auth", "can-i", *request, "--as", user, check=False
            )
            .strip()
            .startswith("yes")
        )

    assert can(controller, "create", "pods", "--namespace", bob_namespace)
    for request in (
        ("create", "pods", "--namespace", "kube-system"),
        ("get", "secrets", "--namespace", "kube-system"),
        ("get", "secrets", "--namespace", system.NAMESPACE),
        ("delete", "namespaces"),
        ("create", "clusterrolebindings"),
        ("delete", "persistentvolumes"),
    ):
        assert not can(controller, *request), f"the controller may {request}"
    for account in ("default",):
        user = f"system:serviceaccount:{system.NAMESPACE}:{account}"
        for request in (
            ("create", "pods", "--namespace", bob_namespace),
            ("list", "namespaces"),
            ("get", "secrets", "--namespace", system.NAMESPACE),
        ):
            assert not can(user, *request), f"{account} may {request}"
    mounted = subprocess.run(
        [
            "docker",
            "exec",
            identifier,
            "kubectl",
            "exec",
            "--namespace",
            bob_namespace,
            "deployment/hermes",
            "-c",
            "hermes",
            "--",
            "test",
            "-e",
            "/var/run/secrets/kubernetes.io/serviceaccount/token",
        ],
        capture_output=True,
        timeout=60,
    )
    assert mounted.returncode != 0, "Hermes holds a Kubernetes token"
    # The execution credential works only against the execution interface.
    token = system.instance_pod_exec(
        identifier,
        bob_namespace,
        "cat /run/hermes/credentials/execution-client-token",
    ).strip()
    for url in (
        f"https://{system.ADMIN}/api/v1/session",
        f"https://{system.PUBLIC}/_platform/me",
    ):
        reply = requests.get(
            url, headers={"Authorization": f"Bearer {token}"}, verify=ca, timeout=20
        )
        assert reply.status_code == 401, f"{url}: {reply.status_code}"

    secret_values = [
        value
        for name, value in passwords.items()
        if name in ("llm", "registry", "directory", *system.SUBJECTS)
    ]
    sources = [
        (system.NAMESPACE, f"deployment/{name}")
        for name in (
            "hermes-manager-api",
            "hermes-manager-controller",
            "hermes-gateway",
        )
    ] + [
        (admin.phase(receipt["instanceId"])["namespace"], "deployment/hermes")
        for receipt in assignments.values()
    ]
    for namespace, target in sources:
        logs = system.kubectl(
            identifier,
            "logs",
            "--namespace",
            namespace,
            target,
            "--all-containers",
            "--tail=-1",
            quiet=True,
        )
        for value in secret_values:
            assert value not in logs, f"a fixture secret appears in {target} logs"


def test_two_owners_reach_their_own_native_instances_through_the_gateway(platform):
    two_owners_reach_their_own_native_instances_through_the_gateway(
        platform.identifier,
        platform.admin,
        platform.ca,
        platform.passwords,
        platform.assignments,
    )


@pytest.mark.security
def test_platform_routing_ignores_native_identity_and_refuses_foreign_tickets(platform):
    platform_routing_ignores_native_identity_and_refuses_foreign_tickets(
        platform.ca, platform.passwords
    )


@pytest.mark.security
def test_chat_settings_and_the_admin_secret_reach_only_the_referencing_instance(
    platform,
):
    chat_settings_and_the_admin_secret_reach_only_the_referencing_instance(
        platform.identifier,
        platform.admin,
        platform.ca,
        platform.passwords,
        platform.assignments,
    )


@pytest.mark.security
def test_uploads_reach_only_the_owners_private_home(platform):
    uploads_reach_only_the_owners_private_home(
        platform.identifier,
        platform.admin,
        platform.ca,
        platform.passwords,
        platform.assignments,
    )


@pytest.mark.security
def test_an_email_change_keeps_ownership_and_the_old_email_gets_nothing(platform):
    an_email_change_keeps_ownership_and_the_old_email_gets_nothing(
        platform.ca, platform.passwords, platform.assignments
    )


@pytest.mark.security
def test_owners_share_a_static_folder(platform):
    owners_share_a_static_folder(
        platform.identifier,
        platform.admin,
        platform.assignments,
        platform.state.template,
        platform.state.terminal,
    )


def test_a_missing_static_directory_blocks_without_a_substitute(platform):
    a_missing_static_directory_blocks_without_a_substitute(
        platform.identifier,
        platform.admin,
        platform.state.template,
        platform.state.terminal,
    )


def test_a_hermes_without_browser_traffic_acquires_a_terminal_backend(platform):
    a_hermes_without_browser_traffic_acquires_a_terminal_backend(
        platform.identifier, platform.admin, platform.assignments
    )


@pytest.mark.security
def test_only_the_controller_holds_kubernetes_credentials_and_logs_hold_no_secrets(
    platform,
):
    only_the_controller_holds_kubernetes_credentials_and_logs_hold_no_secrets(
        platform.identifier,
        platform.admin,
        platform.ca,
        platform.passwords,
        platform.assignments,
    )


def test_published_versions_commission_ready_instances_from_the_api(platform):
    namespaces, instances = set(), set()
    for name, receipt in platform.assignments.items():
        phase = platform.admin.phase(receipt["instanceId"])
        assert phase["phase"] == "ready", (name, phase["phase"])
        namespaces.add(phase["namespace"])
        instances.add(receipt["instanceId"])
    assert len(platform.assignments) == len(namespaces) == len(instances) == 2


def test_a_cron_job_fires_without_browser_traffic_and_acquires_a_terminal(platform):
    a_cron_job_fires_without_browser_traffic_and_acquires_a_terminal(
        platform.identifier,
        platform.admin,
        platform.ca,
        platform.passwords,
        platform.assignments,
    )


def placement(platform, instance_id):
    """Where the instance's Pods and volumes are, and what the scheduler said:
    bounded, from the cluster, and free of credentials."""
    namespace = platform.admin.phase(instance_id)["namespace"]
    cluster = platform.cluster
    shown = [
        cluster.kubectl("get", "pods", "-n", namespace, "-o", "wide", check=False),
        cluster.kubectl(
            "get",
            "pvc,pv",
            "-A",
            "-o",
            "custom-columns=NAME:.metadata.name,NODE:.metadata.annotations.volume\\.kubernetes\\.io/selected-node,STATUS:.status.phase",
            check=False,
        ),
        cluster.kubectl(
            "get",
            "events",
            "-n",
            namespace,
            "--sort-by=.lastTimestamp",
            check=False,
        ),
    ]
    return "\n".join(result.stdout.decode(errors="replace")[-2500:] for result in shown)


def test_bobs_backends_share_the_workspace_and_idle_workers_retire(platform):
    try:
        bobs_backends_share_the_workspace_and_idle_workers_retire(
            platform.identifier, platform.admin, platform.assignments
        )
    except AssertionError as error:
        raise AssertionError(
            f"{str(error)[:600]}\n--- placement ---\n"
            + placement(platform, platform.assignments["bob"]["instanceId"])
        ) from None


def test_a_backend_identity_controls_its_workers_and_keeps_the_hermes_home_private(
    platform,
):
    a_backend_identity_controls_its_workers_and_keeps_the_hermes_home_private(
        platform.identifier,
        platform.admin,
        platform.state.template,
        platform.state.worker,
    )
