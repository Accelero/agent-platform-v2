"""The manager's Rust tests (API, controller and their libraries), by target."""

import pytest

from tests.support import cargo
from tests.support.cargo import FAULTS, WORKER_CASES, binary, lib

pytestmark = [pytest.mark.manager, pytest.mark.rust]

# Library tests of hermes-native that reach real sockets or processes.
NATIVE_IO = (
    "probe::tests::",
    "the_real_initializer_receipt_verifies_for_exactly_its_owner_and_key",
    "a_worker_is_ready_only_with_its_pinned_host_key_and_the_declared_identity",
    "a_worker_that_accepts_the_key_but_never_opens_a_session_breaks_the_contract",
    "a_session_refused_less_often_than_the_attempts_is_a_blip",
    "unusable_inputs_and_unreachable_workers_are_not_ready_yet",
)

UNIT = [
    binary("hermes-architecture", "architecture"),
    binary("hermes-architecture", "traceability"),
    lib("hermes-core"),
    lib("hermes-http"),
    lib("hermes-identity"),
    lib("hermes-kube"),
    lib("hermes-native", skip=NATIVE_IO),
    binary("hermes-render", "render"),
    lib("hermes-wire"),
    binary("hermes-wire", "contract"),
]
INTEGRATION = [
    lib("hermes-store"),
    lib("hermes-native", only=NATIVE_IO),
    *(
        binary("hermes-http", name)
        for name in (
            "admin_api",
            "admin_auth",
            "execution_api",
            "resolver_api",
            "wire_boundaries",
        )
    ),
    *(binary("hermes-identity", name) for name in ("browser", "directory", "tokens")),
    binary("hermes-manager", "process", features=FAULTS),
]
CLUSTER = [
    binary("hermes-kube", "cluster", skip=WORKER_CASES, threads=2),
    binary("hermes-kube", "admission", threads=2),
    # Each case installs cluster-scoped admission objects for its own control
    # namespace; two at once would overwrite each other's.
    binary("hermes-manager", "controller_process", features=FAULTS, threads=1),
]


@pytest.mark.unit
@pytest.mark.parametrize("target", [target.param() for target in UNIT])
def test_unit(target):
    cargo.run(target)


@pytest.mark.integration
@pytest.mark.database
@pytest.mark.parametrize("target", [target.param() for target in INTEGRATION])
def test_integration(target, postgres):
    cargo.run(target, {"DATABASE_URL": postgres.url})


@pytest.mark.integration
@pytest.mark.cluster
@pytest.mark.parametrize("target", [target.param() for target in CLUSTER])
def test_on_kubernetes(target, kubernetes):
    cargo.run(target, kubernetes)
