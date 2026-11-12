"""Session fixtures every test directory shares, loaded by the root conftest.

Each one is provisioned when the first test that needs it runs and torn down
at the end of the session; a test that needs none of them starts nothing.
Everything they create is disposable, labelled with this run and removed by
exact identity.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tests.support import images as image_support
from tests.support.k3s import (
    import_worker_image,
    remove_container,
    start_k3s,
    wait_ready,
)
from tests.support.ownership import (
    FIXTURE,
    POSTGRES,
    RUN,
    Cleanup,
    new_run,
    remove_fixture,
)

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "services/hermes-manager/ui"
POSTGRES_IMAGE = (
    "postgres@sha256:2a0d0fe14825b0939f78a8cad5cd4e6aa68bf94d0e5dd96e24b6d23af4315545"
)


def output(command, timeout=120):
    return subprocess.run(
        command, check=True, capture_output=True, text=True, timeout=timeout
    ).stdout.strip()


class Images:
    """Builds each image under test at most once per session, unless the run
    uses images that are already built or saved (`--prebuilt`)."""

    def __init__(self, prebuilt):
        self.prebuilt = prebuilt
        self.ready = set()
        self.lock = threading.Lock()

    def require(self, *names):
        with self.lock:
            wanted = [name for name in names if name not in self.ready]
            if self.prebuilt:
                for name in wanted:
                    if not image_support.available(image_support.TAGS[name]):
                        raise image_support.ImageUnavailable(
                            f"{image_support.TAGS[name]} is neither built nor saved"
                        )
            else:
                image_support.build_all(wanted)
            self.ready.update(wanted)
        return [image_support.TAGS[name] for name in names]


@pytest.fixture(scope="session")
def images(request):
    return Images(request.config.getoption("--prebuilt"))


@pytest.fixture(scope="session")
def hermes_image(images):
    return images.require("hermes")[0]


@pytest.fixture(scope="session")
def manager_image(images):
    return images.require("manager")[0]


@pytest.fixture(scope="session")
def worker_image(images):
    return images.require("worker")[0]


@pytest.fixture(scope="session")
def postgres():
    """One disposable PostgreSQL on the default bridge, published on loopback."""
    run = new_run()
    name = f"hermes-tests-{run[:8]}"
    with Cleanup() as cleanup:
        cleanup.add(name, lambda: remove_fixture(name, POSTGRES, run))
        output(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                name,
                "--label",
                f"{FIXTURE}={POSTGRES}",
                "--label",
                f"{RUN}={run}",
                "--publish",
                "127.0.0.1::5432",
                "--env",
                "POSTGRES_USER=hermes_test",
                "--env",
                "POSTGRES_DB=hermes_test",
                "--env",
                "POSTGRES_HOST_AUTH_METHOD=trust",
                POSTGRES_IMAGE,
            ],
            timeout=300,
        )
        port = output(["docker", "port", name, "5432/tcp"]).splitlines()[0]
        port = port.rsplit(":", 1)[1]
        ready = ["docker", "exec", name, "pg_isready", "-h", "127.0.0.1"]
        ready += ["-U", "hermes_test", "-d", "hermes_test"]
        deadline = time.monotonic() + 90
        while subprocess.run(ready, capture_output=True, timeout=30).returncode:
            if time.monotonic() > deadline:
                raise RuntimeError("the PostgreSQL fixture did not start")
            time.sleep(1)
        yield SimpleNamespace(
            name=name,
            run=run,
            url=f"postgres://hermes_test@127.0.0.1:{port}/hermes_test",
        )


@pytest.fixture(scope="session")
def kubernetes(postgres, worker_image, tmp_path_factory):
    """A disposable single-server k3s next to the PostgreSQL fixture, with the
    release worker imported; yields the environment the Rust cluster tests
    read."""
    run = new_run()
    with Cleanup() as cleanup:
        identifier = start_k3s("bridge", f"personal-hermes-kube-test-{run}", run)
        cleanup.add("k3s container", lambda: remove_container(identifier, run))
        config = wait_ready(identifier)
        directory = tmp_path_factory.mktemp("k3s")
        directory.chmod(0o700)
        kubeconfig = directory / "kubeconfig"
        kubeconfig.write_text(yaml.safe_dump(config))
        kubeconfig.chmod(0o600)
        yield {
            "DATABASE_URL": postgres.url,
            "HERMES_TEST_KUBECONFIG": str(kubeconfig),
            "HERMES_TEST_K3S_CONTAINER": identifier,
            "HERMES_TEST_POSTGRES_CONTAINER": postgres.name,
            "HERMES_TEST_RUN": run,
            "HERMES_TEST_WORKER_IMAGE": import_worker_image(identifier),
        }


def npm(*arguments, timeout=1800):
    result = subprocess.run(["npm", *arguments], cwd=UI, timeout=timeout)
    assert result.returncode == 0, f"npm {' '.join(arguments)} failed"


@pytest.fixture(scope="session")
def ui_dependencies():
    """The dashboard's locked dependencies, installed once with the pinned Node.

    Only `npm ci` enforces the Node version, so another local Node installs
    through the Node image the manager's Dockerfile pins.
    """
    pinned = (UI / ".node-version").read_text().strip()
    if output(["node", "--version"]).lstrip("v") == pinned:
        npm("ci", "--ignore-scripts")
        return UI
    dockerfile = (ROOT / "services/hermes-manager/Dockerfile").read_text()
    digest = re.search(r"FROM node@(sha256:\w+)", dockerfile)[1]
    result = subprocess.run(
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
        ],
        timeout=1800,
    )
    assert result.returncode == 0, "installing the dashboard's dependencies failed"
    return UI


@pytest.fixture(scope="session")
def ui_dist(ui_dependencies):
    """The production dashboard build, as the manager image serves it."""
    npm("run", "build")
    return UI / "dist"


@pytest.fixture
def unique():
    return uuid.uuid4().hex[:8]
