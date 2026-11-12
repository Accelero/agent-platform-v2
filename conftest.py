"""The repository's one pytest suite: collection rules shared by every test.

Each test has exactly one level marker (`unit`, `integration`, `e2e`). Unit
and integration tests also belong to exactly one component; tests that live
with their component get its marker from their location, and the wrappers in
`tests/services/` name theirs. End-to-end tests cover the installed system
and belong to no single component. A skipped, xfailed or xpassed test fails
the run: a test whose fixture is missing has not passed.
"""

import os

import pytest

pytest_plugins = ["tests.support.fixtures", "tests.support.shards"]

LEVELS = {"unit", "integration", "e2e"}
COMPONENTS = {"manager", "gateway", "dashboard", "hermes", "worker", "chart", "tooling"}
LOCATIONS = {
    "agent_platform/": "tooling",
    "tests/harness/": "tooling",
    "charts/": "chart",
    "contracts/hermes-manager/": "manager",
    "runtimes/hermes/": "hermes",
}


def pytest_addoption(parser):
    parser.addoption(
        "--prebuilt",
        action="store_true",
        help="test the images already built or saved in HERMES_TEST_IMAGE_ARCHIVES",
    )


def names(item, among):
    return {marker.name for marker in item.iter_markers() if marker.name in among}


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    for item in items:
        location = item.path.relative_to(config.rootpath).as_posix()
        for prefix, component in LOCATIONS.items():
            if location.startswith(prefix):
                item.add_marker(component)
        levels, components = names(item, LEVELS), names(item, COMPONENTS)
        if len(levels) != 1:
            raise pytest.UsageError(f"{item.nodeid}: needs exactly one level marker")
        if len(components) != (0 if levels == {"e2e"} else 1):
            raise pytest.UsageError(
                f"{item.nodeid}: a unit or integration test belongs to one "
                "component, an end-to-end test to none"
            )
    # Cases must not depend on each other: run them back to front to prove it.
    if os.environ.get("HERMES_TEST_ORDER") == "reverse":
        items.reverse()


def pytest_sessionfinish(session):
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    stats = reporter.stats if reporter else {}
    unpassed = sum(
        len(stats.get(kind, [])) for kind in ("skipped", "xfailed", "xpassed")
    )
    if unpassed and session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
