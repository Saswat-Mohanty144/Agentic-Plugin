"""authenticate/require: status codes, bodies, headers, freshness, logging (spec §6.6, §6.7, §6.9)."""

import logging

import pytest
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from hrms_plugin.security.http_auth import ApiError, require
from hrms_plugin.security.jwks_cache import JwksCache
from hrms_plugin.security.rbac import AuthContext, Permission
from tests.support.auth import ACME_JWKS, NOW, MutableClock, claims, mint


def _app(verifier, clock):
    app = FastAPI()
    app.state.token_verifier = verifier
    app.state.clock = clock

    @app.exception_handler(ApiError)
    async def _h(_request, exc: ApiError):
        return JSONResponse(status_code=exc.status_code, content=exc.body, headers=exc.headers)

    @app.get("/copilot")
    def copilot(auth: AuthContext = Depends(require(Permission.USE_COPILOT))):
        return {"sub": auth.user_id, "tenant": auth.tenant_id}

    @app.post("/remediate")
    def remediate(auth: AuthContext = Depends(require(Permission.EXECUTE_REMEDIATION, max_token_age_seconds=300))):
        return {"ok": True}

    return app


@pytest.fixture
def client(verifier, clock):
    return TestClient(_app(verifier, clock))


def test_valid_token_passes(client, bearer):
    assert client.get("/copilot", headers=bearer(roles=["staff"])).json() == {"sub": "user-1", "tenant": "acme"}


def test_no_token_401_with_challenge(client):
    res = client.get("/copilot")
    assert res.status_code == 401
    assert res.json() == {"error": "UNAUTHENTICATED"}
    assert res.headers["www-authenticate"] == 'Bearer error="invalid_token"'


def test_duplicate_authorization_headers_401(client, make_token):
    t = make_token()
    res = client.get("/copilot", headers=[("Authorization", f"Bearer {t}"), ("Authorization", f"Bearer {t}")])
    assert res.status_code == 401


def test_missing_permission_403(client, bearer):
    res = client.post("/remediate", headers=bearer(roles=["staff"]))
    assert res.status_code == 403
    assert res.json() == {"error": "FORBIDDEN", "permission": "EXECUTE_REMEDIATION"}


def test_no_mapped_roles_403(client, bearer):
    assert client.get("/copilot", headers=bearer(roles=["janitor"])).status_code == 403


def test_tenant_header_equal_ok_absent_ok_different_403(client, bearer):
    h = bearer()
    assert client.get("/copilot", headers=h).status_code == 200
    assert client.get("/copilot", headers={**h, "X-Tenant-Id": "acme"}).status_code == 200
    res = client.get("/copilot", headers={**h, "X-Tenant-Id": "globex"})
    assert res.status_code == 403 and res.json() == {"error": "TENANT_MISMATCH"}


def test_freshness_boundary(client, bearer):
    assert client.post("/remediate", headers=bearer(iat=NOW - 300)).status_code == 200
    res = client.post("/remediate", headers=bearer(iat=NOW - 301))
    assert res.status_code == 401 and res.json() == {"error": "REAUTH_REQUIRED"}


def test_freshness_applies_only_where_declared(client, bearer):
    assert client.get("/copilot", headers=bearer(iat=NOW - 301)).status_code == 200


def test_forbidden_checked_before_freshness(client, bearer):
    assert client.post("/remediate", headers=bearer(roles=["staff"], iat=NOW - 301)).status_code == 403


def test_body_identical_for_different_failures_but_reasons_logged(client, bearer, keys, caplog):
    caplog.set_level(logging.WARNING, logger="hrms_plugin.security.auth")
    expired = client.get("/copilot", headers=bearer(iat=NOW - 1000))
    forged = mint(keys.initech_rsa, "acme-rsa", claims())
    bad_sig = client.get("/copilot", headers={"Authorization": f"Bearer {forged}"})
    assert expired.json() == bad_sig.json() == {"error": "UNAUTHENTICATED"}
    reasons = {getattr(r, "reason", None) for r in caplog.records}
    assert {"EXPIRED", "BAD_SIGNATURE"} <= reasons


def test_token_never_logged(client, make_token, caplog):
    caplog.set_level(logging.DEBUG)
    token = make_token(iat=NOW - 1000)
    client.get("/copilot", headers={"Authorization": f"Bearer {token}"})
    assert token not in caplog.text
    assert all(token not in str(getattr(r, "__dict__", {})) for r in caplog.records)


def test_jwks_outage_is_503(registry, jwks_fetcher, clock, make_token):
    from hrms_plugin.security.token_verifier import TokenVerifier

    jwks_fetcher.failures.add(ACME_JWKS)
    verifier = TokenVerifier(registry, JwksCache(jwks_fetcher, clock=MutableClock(0.0)), clock=clock)
    res = TestClient(_app(verifier, clock)).get("/copilot", headers={"Authorization": f"Bearer {make_token()}"})
    assert res.status_code == 503 and res.json() == {"error": "AUTH_UNAVAILABLE"}


def test_require_exposes_permission_for_route_audits():
    dep = require(Permission.VIEW_COMPLIANCE, max_token_age_seconds=300)
    assert dep.permission is Permission.VIEW_COMPLIANCE and dep.max_token_age_seconds == 300
