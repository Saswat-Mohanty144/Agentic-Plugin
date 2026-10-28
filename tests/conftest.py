"""Test-wide environment and fixtures. Environment is set before any hrms_plugin module is imported."""

import os
import tempfile
from pathlib import Path

# Never let tests reach a paid LLM: the gateway reads these at construction time.
os.environ["LLM_PROVIDER"] = "fake"
# Keep the SQLite store out of the repository.
os.environ["HRMS_DATA_DIR"] = tempfile.mkdtemp(prefix="hrms-test-data-")
# The app module builds its default app from this registry at import time.
os.environ["HRMS_TENANT_REGISTRY"] = str(Path(__file__).parent / "fixtures" / "tenant_registry.yaml")

import pytest  # noqa: E402

from tests.support.scripted_llm import assert_consumed, scripted_gateway  # noqa: E402


@pytest.fixture
def scripted():
    """Factory for scripted gateways; fails the test on teardown if any scripted response is unused."""
    clients = []

    def _make(*responses):
        gateway, client = scripted_gateway(responses)
        clients.append(client)
        return gateway, client

    yield _make
    assert_consumed(clients)
