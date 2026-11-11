"""Run tests by component and level, and report them the same way.

    uv run python -m tests.run manager unit
    uv run python -m tests.run manager integration
    uv run python -m tests.run manager
    uv run python -m tests.run manager integration worker integration
    uv run python -m tests.run e2e
    uv run python -m tests.run e2e --part 1
    uv run python -m tests.run all

Each fixture is provisioned once per invocation and reused by every suite that
can share it. `--prebuilt` uses images that are already loaded, as CI does.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agent_platform.build import image_build
from agent_platform.privacy import stage_contexts
from tests.personal_hermes.kubernetes import WORKER_TAG

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "services/hermes-manager/ui"
REPORTS = ROOT / ".local/verification"
POSTGRES_IMAGE = (
    "postgres@sha256:2a0d0fe14825b0939f78a8cad5cd4e6aa68bf94d0e5dd96e24b6d23af4315545"
)
MANAGER_IMAGE = "hermes-manager:e2e"
FIXTURE_LABEL = "agent-platform.io/fixture=personal-hermes-postgres"


class Failed(Exception):
    pass


def execute(command, *, cwd=ROOT, env=None, timeout=3600):
    return subprocess.run(command, cwd=cwd, env=env, timeout=timeout).returncode == 0


def output(command):
    return subprocess.run(
        command, check=True, capture_output=True, text=True, timeout=120
    ).stdout.strip()


class Session:
    """The fixtures and results of one invocation."""

    def __init__(self, prebuilt, github_cache=False, part=None):
        self.prebuilt = prebuilt
        self.github_cache = github_cache
        self.part = part
        self.results = {}
        self._postgres = None
        self._built = set()
        self._ui_installed = False

    def record(self, group, passed, cases=0, problems=0):
        total = self.results.setdefault(
            group, {"passed": True, "cases": 0, "problems": 0}
        )
        total["passed"] = total["passed"] and passed
        total["cases"] += cases
        total["problems"] += problems

    def close(self):
        if self._postgres:
            subprocess.run(
                ["docker", "rm", "--force", "--volumes", self._postgres[0]],
                capture_output=True,
                timeout=60,
            )

    def postgres(self):
        """One disposable PostgreSQL for the whole invocation."""
        if not self._postgres:
            name = f"hermes-tests-{uuid.uuid4().hex[:8]}"
            output(
                [
                    "docker",
                    "run",
                    "--detach",
                    "--name",
                    name,
                    "--label",
                    FIXTURE_LABEL,
                    "--publish",
                    "127.0.0.1::5432",
                    "--env",
                    "POSTGRES_USER=hermes_test",
                    "--env",
                    "POSTGRES_DB=hermes_test",
                    "--env",
                    "POSTGRES_HOST_AUTH_METHOD=trust",
                    POSTGRES_IMAGE,
                ]
            )
            port = output(["docker", "port", name, "5432/tcp"]).rsplit(":", 1)[1]
            url = f"postgres://hermes_test@127.0.0.1:{port}/hermes_test"
            self._postgres = (name, url)
            deadline = time.monotonic() + 60
            ready = ["docker", "exec", name, "pg_isready", "-U", "hermes_test"]
            ready += ["-d", "hermes_test"]
            while subprocess.run(ready, capture_output=True).returncode:
                if time.monotonic() > deadline:
                    raise Failed("the PostgreSQL fixture did not start")
                time.sleep(1)
        return self._postgres

    def build(self, *images):
        """Builds each image at most once, and the requested ones side by side."""
        wanted = [] if self.prebuilt else [i for i in images if i not in self._built]
        with ThreadPoolExecutor(max_workers=max(len(wanted), 1)) as pool:
            list(pool.map(self.build_one, wanted))

    def build_one(self, image):
        if image == "worker":
            self.build_worker()
            return
        commands = {
            "hermes": [
                sys.executable,
                "-m",
                "tests.personal_hermes.native_runtime",
                "--build",
                "--build-only",
            ],
            "manager": [
                sys.executable,
                "-m",
                "agent_platform.manager_build",
                "--tag",
                MANAGER_IMAGE,
            ],
        }
        cache = ["--github-cache"] if self.github_cache else []
        if not execute([*commands[image], *cache]):
            raise Failed(f"building the {image} image failed")
        self._built.add(image)

    def build_worker(self):
        """The release worker, from its reviewed context, as the tests import it."""
        with tempfile.TemporaryDirectory(prefix="worker-build-") as directory:
            context = stage_contexts(ROOT, Path(directory) / "inputs")["agent-runtime"]
            command = image_build("agent-runtime", github_cache=self.github_cache)
            if not execute([*command, "--tag", WORKER_TAG, str(context)]):
                raise Failed("building the worker image failed")
        self._built.add("worker")

    def suite(self, group, suite, *, database=False, env=None):
        environment = {**os.environ, **(env or {})}
        if database:
            environment["DATABASE_URL"] = self.postgres()[1]
        report = REPORTS / f"{suite}.json"
        command = [sys.executable, "-m", "tests.support.verification", "run", suite]
        ok = execute([*command, "--output", str(report)], env=environment)
        self.collect(group, suite, ok)

    def collect(self, group, suite, ok):
        report = REPORTS / f"{suite}.json"
        if not report.exists():
            self.record(group, False, problems=1)
            return
        data = json.loads(report.read_text())
        self.record(group, ok, len(data["cases"]), len(data["problems"]))

    def cluster(self, suites):
        """Runs Kubernetes suites together, on one disposable cluster."""
        for _group, suite in suites:
            (REPORTS / f"{suite}.json").unlink(missing_ok=True)
        command = [
            sys.executable,
            "-m",
            "tests.personal_hermes.kubernetes",
            "--postgres-container",
            self.postgres()[0],
        ]
        for _group, suite in suites:
            command.extend(["--suite", suite])
        execute(command)
        for group, suite in suites:
            report = REPORTS / f"{suite}.json"
            passed = report.exists() and not json.loads(report.read_text())["problems"]
            self.collect(group, suite, passed)

    def npm(self, *arguments):
        return execute(["npm", *arguments], cwd=UI)

    def npm_install(self):
        """Installs with the pinned Node; only install enforces its version."""
        pinned = (UI / ".node-version").read_text().strip()
        if output(["node", "--version"]).lstrip("v") == pinned:
            return self.npm("ci", "--ignore-scripts")
        dockerfile = (ROOT / "services/hermes-manager/Dockerfile").read_text()
        digest = re.search(r"FROM node@(sha256:\w+)", dockerfile)[1]
        return execute(
            [
                "docker",
                "run",
                "--rm",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "--env",
                "HOME=/tmp",
                "--env",
                "npm_config_cache=/tmp/npm-cache",
                "--mount",
                f"type=bind,src={UI},dst=/ui",
                "--workdir",
                "/ui",
                f"node@{digest}",
                "npm",
                "ci",
                "--ignore-scripts",
            ]
        )

    def ui_install(self):
        if not self._ui_installed:
            if not self.npm_install():
                raise Failed("installing the dashboard's dependencies failed")
            self._ui_installed = True

    def dashboard_unit(self):
        self.ui_install()
        steps = ("check:generated", "check:schemas", "format:check", "test")
        self.record("dashboard unit", all(self.npm("run", step) for step in steps))

    def dashboard_integration(self):
        self.ui_install()
        built = self.npm("run", "build")
        browser = built and self.npm("run", "test:e2e")
        self.record("dashboard integration", browser)
        if built:
            self.suite(
                "dashboard integration",
                "dashboard-integration",
                database=True,
                env={"MANAGER_UI_DIST": str(UI / "dist")},
            )

    def hermes_integration(self):
        self.build("hermes")
        proof = [sys.executable, "-m", "tests.personal_hermes.native_runtime"]
        self.record("hermes integration", execute(proof))
        self.suite(
            "hermes integration",
            "hermes-integration",
            env={"HERMES_TEST_NATIVE_IMAGE": "personal-hermes-native-test:local"},
        )

    def e2e(self):
        self.build("hermes", "manager")
        command = [
            sys.executable,
            "-m",
            "tests.personal_hermes.native_e2e",
            "--postgres-container",
            self.postgres()[0],
        ]
        if self.part:
            command.extend(["--part", self.part])
        self.record("e2e", execute(command, timeout=7200))


def plain(group, suite, **options):
    return lambda session: session.suite(group, suite, **options)


CLUSTER_SUITES = {
    ("manager", "integration"): [
        ("manager integration", "manager-integration-cluster")
    ],
    ("worker", "integration"): [("worker integration", "worker-integration")],
}
GROUPS = {
    ("tooling", "unit"): plain("tooling unit", "tooling-unit"),
    ("tooling", "integration"): plain("tooling integration", "tooling-integration"),
    ("chart", "integration"): plain("chart integration", "chart-integration"),
    ("hermes", "unit"): plain("hermes unit", "hermes-unit"),
    ("hermes", "integration"): Session.hermes_integration,
    ("dashboard", "unit"): Session.dashboard_unit,
    ("dashboard", "integration"): Session.dashboard_integration,
    ("gateway", "unit"): plain("gateway unit", "gateway-unit"),
    ("gateway", "integration"): plain(
        "gateway integration", "gateway-integration", database=True
    ),
    ("manager", "unit"): plain("manager unit", "manager-unit"),
    ("manager", "integration"): plain(
        "manager integration", "manager-integration", database=True
    ),
    ("worker", "integration"): lambda session: None,
}
COMPONENTS = sorted({component for component, _level in GROUPS})


def selection(tokens):
    """Reads `component [level]` pairs, so one invocation can share its fixtures."""
    chosen, with_e2e, position = [], False, 0
    while position < len(tokens):
        component = tokens[position]
        position += 1
        if component == "all":
            chosen.extend(GROUPS)
            with_e2e = True
        elif component == "e2e":
            with_e2e = True
        elif component in COMPONENTS:
            level = None
            if position < len(tokens) and tokens[position] in ("unit", "integration"):
                level = tokens[position]
                position += 1
            matches = [
                key for key in GROUPS if key[0] == component and level in (None, key[1])
            ]
            if not matches:
                raise SystemExit(f"{component} has no {level} tests")
            chosen.extend(matches)
        else:
            raise SystemExit(f"unknown test group: {component}")
    return list(dict.fromkeys(chosen)), with_e2e


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "groups", nargs="+", metavar="group", help="component [level], e2e, all, build"
    )
    parser.add_argument("--prebuilt", action="store_true")
    parser.add_argument("--github-cache", action="store_true")
    parser.add_argument(
        "--part", choices=("1", "2"), help="run half of e2e, as CI does on two runners"
    )
    args = parser.parse_args()
    REPORTS.mkdir(parents=True, exist_ok=True, mode=0o700)
    session = Session(args.prebuilt, args.github_cache, args.part)
    try:
        if args.groups == ["build"]:
            session.build("hermes", "manager", "worker")
            return 0
        chosen, with_e2e = selection(args.groups)
        for key in chosen:
            GROUPS[key](session)
        cluster = [suite for key in chosen for suite in CLUSTER_SUITES.get(key, [])]
        if cluster:
            session.cluster(cluster)
        if with_e2e:
            session.e2e()
    except Failed as error:
        print(f"tests.run: {error}", flush=True)
        session.record(" ".join(args.groups), False)
    finally:
        session.close()
    print("\n== results", flush=True)
    for group, result in session.results.items():
        counts = f" ({result['cases']} cases)" if result["cases"] else ""
        if result["passed"]:
            print(f"{group}: passed{counts}")
        else:
            failed = f" ({result['problems']} of {result['cases']})" if counts else ""
            print(f"{group}: FAILED{failed}")
    return int(not all(result["passed"] for result in session.results.values()))


if __name__ == "__main__":
    raise SystemExit(main())
