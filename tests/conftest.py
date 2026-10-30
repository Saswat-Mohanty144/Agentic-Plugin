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


from tests.support.auth import (  # noqa: E402
    ACME_JWKS,
    INITECH_JWKS,
    NOW,
    FakeJwksFetcher,
    MutableClock,
    acme_jwks,
    claims,
    generate_test_keys,
    mint,
    public_jwk,
)

_KEYS = generate_test_keys()


@pytest.fixture(scope="session")
def keys():
    return _KEYS


@pytest.fixture(scope="session", autouse=True)
def audit_signing_env(tmp_path_factory):
    """Session-wide Ed25519 audit signing key (piece D7). Tests unset via monkeypatch for unconfigured cases."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key_dir = tmp_path_factory.mktemp("audit_signing_key")
    key_file = key_dir / "audit_ed25519.pem"
    priv_key = Ed25519PrivateKey.generate()
    pem = priv_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key_file.write_bytes(pem)
    key_id = "test-session-audit-key-1"
    os.environ["HRMS_AUDIT_SIGNING_KEY_FILE"] = str(key_file)
    os.environ["HRMS_AUDIT_KEY_ID"] = key_id
    yield {"key_file": str(key_file), "key_id": key_id, "private_key": priv_key}


@pytest.fixture
def registry():
    from hrms_plugin.security.tenant_registry import load_registry

    return load_registry(Path(__file__).parent / "fixtures" / "tenant_registry.yaml")


@pytest.fixture
def jwks_fetcher(keys):
    return FakeJwksFetcher(
        {ACME_JWKS: acme_jwks(keys), INITECH_JWKS: {"keys": [public_jwk(keys.initech_rsa, "initech-rsa")]}}
    )


@pytest.fixture
def clock():
    return MutableClock(NOW)


@pytest.fixture
def verifier(registry, jwks_fetcher, clock):
    from hrms_plugin.security.jwks_cache import JwksCache
    from hrms_plugin.security.token_verifier import TokenVerifier

    return TokenVerifier(registry, JwksCache(jwks_fetcher, clock=MutableClock(0.0)), clock=clock)


@pytest.fixture
def make_token(keys):
    def _make(**kw):
        return mint(keys.rsa, "acme-rsa", claims(**kw))

    return _make


@pytest.fixture
def bearer(make_token):
    def _headers(**kw):
        return {"Authorization": f"Bearer {make_token(**kw)}"}

    return _headers


@pytest.fixture
def make_client(registry, jwks_fetcher, clock):
    from fastapi.testclient import TestClient

    from hrms_plugin.security.jwks_cache import JwksCache
    from hrms_plugin.server.app import create_app

    def _make(supervisor_agent=None, env=None, raise_server_exceptions=True):
        app = create_app(registry=registry, jwks_cache=JwksCache(jwks_fetcher, clock=MutableClock(0.0)),
                         clock=clock, supervisor_agent=supervisor_agent, env=env or {})
        return TestClient(app, raise_server_exceptions=raise_server_exceptions)

    return _make
