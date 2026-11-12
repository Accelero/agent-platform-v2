"""The `agent-runtime` worker image on a disposable Kubernetes API."""

import pytest

from tests.support import cargo
from tests.support.cargo import WORKER_CASES, binary

pytestmark = [pytest.mark.worker, pytest.mark.rust]


@pytest.mark.integration
@pytest.mark.cluster
def test_the_worker_image_serves_ssh_on_kubernetes(kubernetes):
    cargo.run(
        binary("hermes-kube", "cluster", only=WORKER_CASES, threads=2), kubernetes
    )
