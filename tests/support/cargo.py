"""Rust test targets as pytest cases: one case per `cargo test` target.

Cargo and libtest stay the runners. A case passes when cargo exits
successfully, at least one Rust test ran, and libtest ignored none: a test
marked `#[ignore]` runs where its fixture exists (`--include-ignored`), so it
can never be silently left out. A failure shows libtest's own failure section.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

WORKSPACE = Path(__file__).resolve().parents[2] / "services/hermes-manager"
FAULTS = ("--features", "fault-injection")
# The cluster suite's cases that prove the worker image, not the manager.
WORKER_CASES = (
    "a_worker_starts_with_the_instance_keys_and_answers_ssh_as_the_data_identity",
    "a_worker_whose_image_cannot_be_pulled_is_reported_as_unavailable",
)


@dataclass(frozen=True)
class Target:
    """`cargo test -p <package> <selection>`, with libtest filters."""

    package: str
    selection: tuple[str, ...]
    features: tuple[str, ...] = ()
    only: tuple[str, ...] = ()
    skip: tuple[str, ...] = ()
    threads: int | None = None
    marks: tuple = field(default=(), compare=False)

    @property
    def name(self):
        return " ".join([self.package, *self.selection]) + (
            " (selected cases)" if self.only else ""
        )

    def param(self):
        return pytest.param(self, id=self.name, marks=self.marks)


def lib(package, **options):
    return Target(package, ("--lib",), **options)


def binary(package, name, **options):
    return Target(package, ("--test", name), **options)


def results(output):
    totals = {"passed": 0, "failed": 0, "ignored": 0}
    for line in re.findall(r"^test result: .*$", output, re.MULTILINE):
        for count, kind in re.findall(r"(\d+) (passed|failed|ignored)", line):
            totals[kind] += int(count)
    return totals


def run(target, env=None, timeout=2400):
    libtest = ["--color=never", "--include-ignored"]
    if target.threads:
        libtest += ["--test-threads", str(target.threads)]
    for part in target.skip:
        libtest += ["--skip", part]
    command = ["cargo", "test", "--locked", "-p", target.package, *target.features]
    command += [*target.selection, "--", *libtest, *target.only]
    result = subprocess.run(
        command,
        cwd=WORKSPACE,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    totals = results(result.stdout)
    if result.returncode or totals["failed"]:
        failures = result.stdout.split("\nfailures:\n", 1)[-1][-20000:]
        pytest.fail(
            f"cargo test {target.name} failed\n{failures}\n{result.stderr[-3000:]}",
            pytrace=False,
        )
    assert totals["passed"], f"cargo test {target.name} ran no test"
    assert not totals["ignored"], f"cargo test {target.name} ignored a test"
