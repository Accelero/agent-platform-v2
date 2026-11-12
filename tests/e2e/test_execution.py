"""Terminal backend lifecycle on a platform of its own: the scenario changes
the owner's backend versions, so it is `destructive`."""

import json

import pytest

from tests.support import system

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.cluster,
    pytest.mark.destructive,
    pytest.mark.slow,
]


CHANGE_SCRIPT = (
    system.PLUGIN_PRELUDE
    + """
held = environment("", "e2e-held")
codes = [held.execute("echo first")["returncode"]]
print(json.dumps({"held": held.managed_environment}), flush=True)
go = Path("/tmp/e2e-change-go")
deadline = time.monotonic() + 300
while not go.exists() and time.monotonic() < deadline:
    time.sleep(1)
codes.append(held.execute("echo second")["returncode"])
fresh = environment("", "e2e-fresh")
codes.append(fresh.execute("echo third")["returncode"])
result = {"held": held.managed_environment, "fresh": fresh.managed_environment,
          "codes": codes}
held.cleanup()
fresh.cleanup()
print(json.dumps(result))
"""
)


def a_backend_change_keeps_active_leases_and_drains_superseded_environments(
    identifier, admin, assignments, worker, versions
):
    """A new backend version during an active lease: the lease keeps its
    environment, new acquisitions use the new version, and the superseded
    environment drains once its lease ends."""
    instance = assignments["bob"]["instanceId"]
    namespace = admin.phase(instance)["namespace"]
    pod = system.hermes_pod(identifier, namespace)
    process = system.hermes_python_started(identifier, namespace, CHANGE_SCRIPT)
    try:
        held = json.loads(process.stdout.readline())["held"]
        generation = admin.phase(instance)["executionGeneration"]
        version = admin.publish(
            "terminal-backends",
            "shell",
            {
                **worker,
                "resources": {**worker["resources"], "memoryMib": 384},
                "limits": {
                    "idleSeconds": 300,
                    "maxLeaseSeconds": 900,
                    "maxEnvironments": 4,
                },
                "mounts": {"workspace": {"mountPath": "/workspace"}},
            },
        )
        system.change_terminal(
            admin,
            assignments["bob"]["assignmentId"],
            lambda terminal: terminal["allowed"].update(shell=version),
        )
        system.wait(
            lambda: admin.phase(instance)["executionGeneration"] > generation,
            60,
            "the backend change did not advance the execution generation",
            interval=1,
        )
        system.instance_pod_exec(identifier, namespace, "touch /tmp/e2e-change-go")
        output, _ = process.communicate(timeout=400)
    finally:
        if process.poll() is None:
            process.kill()
    result = json.loads(output.strip().splitlines()[-1])
    assert result["codes"] == [0, 0, 0], result
    assert result["held"] == held != result["fresh"], result
    listed = {e["id"]: e for e in system.environments(admin, instance)}
    assert listed[held]["backend"] == {"id": "shell", "version": versions["shell"]}
    assert listed[result["fresh"]]["backend"] == {"id": "shell", "version": version}
    system.wait(
        lambda: (
            {e["id"]: e["state"] for e in system.environments(admin, instance)}[held]
            == "retired"
        ),
        360,
        "the superseded environment never drained",
        interval=5,
    )
    assert system.hermes_pod(identifier, namespace) == pod, (
        "a backend change restarted Hermes"
    )
    versions["shell"] = version


def test_a_backend_change_keeps_active_leases_and_drains_superseded_environments(
    destructive_platform,
):
    platform = destructive_platform
    a_backend_change_keeps_active_leases_and_drains_superseded_environments(
        platform.identifier,
        platform.admin,
        platform.assignments,
        platform.state.worker,
        platform.state.versions,
    )
