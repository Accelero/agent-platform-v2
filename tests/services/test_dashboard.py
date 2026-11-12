"""The React dashboard: Vitest and its checks, Playwright, and the production
build served by the manager under its real CSP."""

import pytest

from tests.support import cargo
from tests.support.cargo import binary
from tests.support.fixtures import npm

pytestmark = [pytest.mark.dashboard, pytest.mark.node]


@pytest.mark.unit
@pytest.mark.parametrize(
    "script", ["check:generated", "check:schemas", "format:check", "test"]
)
def test_unit(script, ui_dependencies):
    npm("run", script)


@pytest.mark.integration
@pytest.mark.browser
def test_workflows_in_a_browser(ui_dist):
    npm("run", "test:e2e")


@pytest.mark.integration
@pytest.mark.browser
@pytest.mark.database
@pytest.mark.rust
def test_the_production_build_under_the_manager_csp(ui_dist, postgres):
    cargo.run(
        binary("hermes-http", "dashboard_browser"),
        {"DATABASE_URL": postgres.url, "MANAGER_UI_DIST": str(ui_dist)},
    )
