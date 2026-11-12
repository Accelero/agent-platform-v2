"""Phase 0: real native OIDC without the owner pin, on an isolated network.

The TLS relay models the gateway's forwarded Host; it does not implement platform
authentication. Actual gateway/browser acceptance belongs to SH-E05 and SH-E18.
"""

import json

import pytest

from tests.support.native import CommandFailed
from tests.support.native_shared import PROVIDER_SHA256, SharedNative
from tests.support.native_shared import test_image as build_test_image
from tests.support.ownership import Cleanup

pytestmark = [
    pytest.mark.integration,
    pytest.mark.image,
    pytest.mark.usefixtures("hermes_image"),
]


@pytest.fixture(scope="module")
def runtime_image():
    with Cleanup() as cleanup:
        yield build_test_image(cleanup)


# One Keycloak and Hermes pair per configuration, reused across its cases.
@pytest.fixture(scope="module")
def shared(tmp_path_factory, runtime_image):
    with Cleanup() as cleanup:
        yield SharedNative(tmp_path_factory.mktemp("shared"), runtime_image, cleanup)


@pytest.fixture(scope="module")
def rotating(tmp_path_factory, runtime_image):
    with Cleanup() as cleanup:
        yield SharedNative(
            tmp_path_factory.mktemp("rotating"), runtime_image, cleanup, rotate=True
        )


@pytest.fixture
def callbacks(tmp_path, runtime_image):
    with Cleanup() as cleanup:
        yield SharedNative(tmp_path, runtime_image, cleanup, unregistered=True)


@pytest.fixture
def personal_native(tmp_path, runtime_image):
    with Cleanup() as cleanup:
        yield SharedNative(tmp_path, runtime_image, cleanup, personal=True)


@pytest.fixture
def retained_providers(tmp_path, runtime_image):
    with Cleanup() as cleanup:
        yield SharedNative(
            tmp_path, runtime_image, cleanup, seed_providers=True, expose_probe=True
        )


def test_two_users_log_in_and_hold_native_websockets_on_one_instance(shared):
    """SH-N01: distinct real subjects, simultaneously authenticated on /api/ws."""
    shared.check("two_users")


def test_upstream_login_keeps_the_terminal_selector_and_home_binding(personal_native):
    """SH-N02: non-owner login; exact upstream provider; foreign data refused."""
    shared = personal_native
    assert shared.check("source").decode().strip() == PROVIDER_SHA256
    shared.check("selector")
    shared.check("two_users")
    layout = shared.layouts[0]
    context = layout.root / "owner/context.json"
    receipt = layout.root / "home/.agent-platform/native-init-receipt.json"
    layout.hand_to("runner")
    original, proof = context.read_bytes(), receipt.read_bytes()
    layout.hand_to("pod")
    foreign = json.loads(original)
    foreign["owner"]["subject"] = "foreign-owner"
    try:
        context.write_text(json.dumps(foreign))
        with pytest.raises(CommandFailed):
            layout.initialize()
        assert layout.result.read_text() == "native_home_owner_conflict"
        layout.hand_to("runner")
        assert receipt.read_bytes() == proof
        layout.hand_to("pod")
    finally:
        context.write_bytes(original)


@pytest.mark.parametrize(
    "fixture", ["shared", "rotating"], ids=["refresh-reuse", "refresh-rotation"]
)
def test_sso_refresh_and_logout_across_two_agent_hosts(fixture, request):
    """SH-N04: shared SSO, independent refresh chains and person-scoped logout."""
    request.getfixturevalue(fixture).check("parallel", timeout=120)


@pytest.mark.parametrize(
    "mode",
    ["native_only", "gateway_retained", "both_revoked"],
    ids=["native-only", "gateway-retained", "both-revoked"],
)
def test_keycloak_sso_depends_on_remaining_client_sessions(shared, mode):
    """Supplemental Phase 0 evidence, not a replacement for SH-N04."""
    shared.check(f"provider_{mode}")


def test_exact_callbacks_and_native_features_through_the_forwarded_host(callbacks):
    """SH-N05: exact callback, unregistered refusal and native API functionality."""
    callbacks.check("callback")


def test_retained_configuration_cannot_add_native_login_providers(retained_providers):
    """SH-N03: only rendered self-hosted login survives the managed layer."""
    retained_providers.check("providers")
    retained_providers.check_probe()
