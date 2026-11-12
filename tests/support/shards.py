"""`--shard K/N`: run one of N balanced parts of the selected tests.

Whole test files go to a part, so the cases of a file share one part's
fixtures. Parts are balanced by the recorded seconds per file in
`tests/e2e/durations.json`, plus the setup of each session fixture the part
needs for the first time (a platform takes minutes to install). The split is
deterministic: every runner computes the same parts from the same selection.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

import pytest

DURATIONS = Path(__file__).resolve().parents[1] / "e2e/durations.json"
# A file without a recorded duration counts this much per case.
DEFAULT_CASE_SECONDS = 60
# The setup each fixture costs once per part that uses it.
SETUP_SECONDS = {"platform": 240, "destructive_pool": 240, "cluster": 150}
# Fixtures that never share a part when there are parts enough: each is a whole
# installed platform, and a CI runner holds one.
EXCLUSIVE = ({"platform"}, {"destructive_pool"})


def pytest_addoption(parser):
    parser.addoption(
        "--shard",
        metavar="K/N",
        help="run part K of N of the selected tests, split by whole files",
    )


def parse(value):
    matched = re.fullmatch(r"(\d+)/(\d+)", value)
    if not matched or not 1 <= int(matched[1]) <= int(matched[2]):
        raise pytest.UsageError("--shard takes K/N with 1 <= K <= N")
    return int(matched[1]), int(matched[2])


def split(files, count):
    """Assigns {file: (seconds, fixtures)} to `count` parts, longest first, each
    to the part it finishes soonest in; returns a list of file sets."""
    parts = [{"seconds": 0, "fixtures": set(), "files": set()} for _ in range(count)]

    def cost(part, file):
        seconds, fixtures = files[file]
        new = fixtures - part["fixtures"]
        return part["seconds"] + seconds + sum(SETUP_SECONDS[name] for name in new)

    def allowed(part, file):
        """A part that holds one exclusive fixture takes no file needing another."""
        held = [group for group in EXCLUSIVE if group & part["fixtures"]]
        wanted = [group for group in EXCLUSIVE if group & files[file][1]]
        return not held or all(group in held for group in wanted)

    for file in sorted(files, key=lambda name: (-files[name][0], name)):
        candidates = [part for part in parts if allowed(part, file)] or parts
        part = min(candidates, key=lambda candidate: cost(candidate, file))
        part["seconds"] = cost(part, file)
        part["fixtures"] |= files[file][1]
        part["files"].add(file)
    return [part["files"] for part in parts]


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    value = config.getoption("--shard")
    if not value:
        return
    index, count = parse(value)
    recorded = json.loads(DURATIONS.read_text()) if DURATIONS.exists() else {}
    cases, fixtures = defaultdict(int), defaultdict(set)
    for item in items:
        file = item.nodeid.split("::", 1)[0]
        cases[file] += 1
        fixtures[file] |= set(item.fixturenames) & set(SETUP_SECONDS)
    files = {
        file: (recorded.get(file, cases[file] * DEFAULT_CASE_SECONDS), fixtures[file])
        for file in cases
    }
    chosen = split(files, count)[index - 1]
    kept = [item for item in items if item.nodeid.split("::", 1)[0] in chosen]
    deselected = [item for item in items if item.nodeid.split("::", 1)[0] not in chosen]
    if deselected:
        config.hook.pytest_deselected(items=deselected)
    items[:] = kept
