"""Every `system.<name>` the cases use exists, so a moved or dropped helper
fails here instead of at the first case of an hour-long run."""

import re
from pathlib import Path

import pytest

from tests.support import system

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
SOURCES = sorted((ROOT / "tests").rglob("*.py"))


@pytest.mark.parametrize("path", SOURCES, ids=lambda path: str(path.relative_to(ROOT)))
def test_every_system_helper_a_module_uses_exists(path):
    used = set(re.findall(r"(?<![\w.])system\.(\w+)", path.read_text()))
    assert {name for name in used if not hasattr(system, name)} == set()
