"""`hermes-proxy`, `hermes-gateway` and gateway TLS, by Rust test target."""

import pytest

from tests.support import cargo
from tests.support.cargo import binary, lib

pytestmark = [pytest.mark.gateway, pytest.mark.rust]

INTEGRATION = [
    binary("hermes-proxy", "gateway"),
    binary("hermes-proxy", "resolver_client"),
    binary("hermes-gateway", "gateway"),
    binary("hermes-http", "gateway_tls"),
]


@pytest.mark.unit
def test_unit():
    cargo.run(lib("hermes-proxy"))


@pytest.mark.integration
@pytest.mark.database
@pytest.mark.parametrize("target", [target.param() for target in INTEGRATION])
def test_integration(target, postgres):
    cargo.run(target, {"DATABASE_URL": postgres.url})
