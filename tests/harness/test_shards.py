"""`--shard K/N` splits the selected files so every runner gets its share."""

import pytest

from tests.support.shards import SETUP_SECONDS, parse, split

pytestmark = pytest.mark.unit


def test_every_file_lands_in_exactly_one_part_and_the_split_is_stable():
    files = {f"test_{index}.py": (index * 10, set()) for index in range(1, 12)}
    parts = split(files, 4)
    assert sorted(name for part in parts for name in part) == sorted(files)
    assert parts == split(dict(reversed(files.items())), 4)


def test_long_files_spread_over_the_parts():
    files = {"a": (600, set()), "b": (600, set()), "c": (100, set())}
    first, second = split(files, 2)
    assert ("a" in first) != ("b" in first)
    assert len(first) + len(second) == 3


def test_a_fixture_costs_its_setup_once_per_part():
    """Two files on the same platform share one install; a third file needing
    another fixture goes where that setup is cheapest."""
    platform = {"platform"}
    files = {
        "first": (100, platform),
        "second": (100, platform),
        "pool": (SETUP_SECONDS["destructive_pool"], {"destructive_pool"}),
    }
    parts = split(files, 2)
    together = next(part for part in parts if "first" in part)
    assert "second" in together and "pool" not in together


@pytest.mark.parametrize("value", ["0/3", "4/3", "3", "a/b", "1/0"])
def test_a_malformed_shard_is_refused(value):
    with pytest.raises(pytest.UsageError):
        parse(value)
