"""Every route except /healthz requires its declared permission (spec §6.4, §6.6, §6.8, AM2, AM5)."""

import pytest

from hrms_plugin.security.jwks_cache import JwksCache
from hrms_plugin.security.rbac import Permission as P
from hrms_plugin.server.app import create_app
from tests.support.routes import api_routes

EXPECTED = {
    ("POST", "/v1/chat"): P.USE_COPILOT,
    ("POST", "/v1/chat/stream"): P.USE_COPILOT,
    ("POST", "/api/compliance/agent/chat"): P.USE_COPILOT,
    ("POST", "/v1/voice/transcribe"): P.USE_COPILOT,
    ("POST", "/v1/voice/synthesize"): P.USE_COPILOT,
    ("POST", "/v1/voice/turn"): P.USE_COPILOT,
    ("POST", "/v1/schema/introspect"): P.MANAGE_SCHEMA,
    ("POST", "/v1/schema/synthesize"): P.MANAGE_SCHEMA,
    ("POST", "/v1/schema/synthesize/agentic"): P.MANAGE_SCHEMA,
    ("GET", "/v1/schema/mappings"): P.MANAGE_SCHEMA,
    ("GET", "/v1/schema/mappings/{vendor}/{entity_type}"): P.MANAGE_SCHEMA,
    ("POST", "/v1/remediate/execute"): P.EXECUTE_REMEDIATION,
    ("POST", "/v1/schema/fingerprints/approve"): P.MANAGE_SCHEMA,
    ("GET", "/v1/schema/fingerprints/quarantine"): P.MANAGE_SCHEMA,
    ("POST", "/v1/writeback/proposals"): P.MUTATE_EMPLOYEE,
    ("GET", "/v1/governance/llm-egress"): P.VIEW_COMPLIANCE,
    ("POST", "/v1/writeback/proposals/{proposal_id}/approve"): P.MUTATE_EMPLOYEE,
    ("POST", "/v1/writeback/proposals/{proposal_id}/rollback"): P.MUTATE_EMPLOYEE,
    ("GET", "/v1/compliance/diagnose"): P.VIEW_COMPLIANCE,
    ("GET", "/api/compliance/agent/diagnose"): P.VIEW_COMPLIANCE,
    ("GET", "/v1/compliance/dashboard/live"): P.VIEW_COMPLIANCE,
    ("GET", "/api/compliance/dashboard/live"): P.VIEW_COMPLIANCE,
    ("POST", "/api/requisitions/generate-archetypes"): P.MANAGE_RECRUITMENT,
    ("POST", "/v1/requisitions/generate-archetypes"): P.MANAGE_RECRUITMENT,
}
CONCRETE = {
    "/v1/schema/mappings/{vendor}/{entity_type}": "/v1/schema/mappings/zoho/EMPLOYEE",
    "/v1/writeback/proposals/{proposal_id}/approve": "/v1/writeback/proposals/p1/approve",
    "/v1/writeback/proposals/{proposal_id}/rollback": "/v1/writeback/proposals/p1/rollback",
}
# Role in the fixture registry that LACKS / HOLDS each permission.
LACKING = {
    P.MUTATE_EMPLOYEE: "payroll_officer",
    P.USE_COPILOT: "janitor",
    P.MANAGE_SCHEMA: "hr_manager",
    P.EXECUTE_REMEDIATION: "staff",
    P.VIEW_COMPLIANCE: "staff",
    P.MANAGE_RECRUITMENT: "staff",
}
HOLDING = {
    P.MUTATE_EMPLOYEE: "hr_manager",
    P.USE_COPILOT: "staff",
    P.MANAGE_SCHEMA: "platform_admin",
    P.EXECUTE_REMEDIATION: "hr_manager",
    P.VIEW_COMPLIANCE: "hr_manager",
    P.MANAGE_RECRUITMENT: "recruiter",
}


def _routes(app):
    out = {}
    for route in api_routes(app.routes):
        perms = [getattr(d.call, "permission", None) for d in route.dependant.dependencies]
        for method in route.methods:
            out[(method, route.path)] = [p for p in perms if p is not None]
    return out


def _app(registry):
    return create_app(registry=registry, jwks_cache=JwksCache(lambda url: None), env={})


def test_route_table_matches_exactly(registry):
    routes = _routes(_app(registry))
    public = {k for k, v in routes.items() if not v}
    assert public == {("GET", "/healthz")}
    assert {k: v[0] for k, v in routes.items() if v} == EXPECTED
    assert all(len(v) <= 1 for v in routes.values())


def test_remediation_route_requires_fresh_token(registry):
    route = next(r for r in api_routes(_app(registry).routes) if r.path == "/v1/remediate/execute")
    ages = [d.call.max_token_age_seconds for d in route.dependant.dependencies if hasattr(d.call, "permission")]
    assert ages == [300]


def _call(client, method, path, headers):
    path = CONCRETE.get(path, path)
    return client.request(method, path, headers=headers, json={} if method == "POST" else None)


@pytest.mark.parametrize("method,path", sorted(EXPECTED))
def test_no_token_401(make_client, method, path):
    assert _call(make_client(), method, path, {}).status_code == 401


@pytest.mark.parametrize("method,path", sorted(EXPECTED))
def test_lacking_permission_403(make_client, bearer, method, path):
    res = _call(make_client(), method, path, bearer(roles=[LACKING[EXPECTED[(method, path)]]]))
    assert res.status_code == 403


@pytest.mark.parametrize("method,path", sorted(EXPECTED))
def test_holding_permission_passes_auth(make_client, bearer, method, path):
    # Body {} may be invalid for the route (422) and a handler may have its own bugs (see the xfail below);
    # the point here is only that auth no longer blocks the request.
    client = make_client(raise_server_exceptions=False)
    res = _call(client, method, path, bearer(roles=[HOLDING[EXPECTED[(method, path)]]]))
    assert res.status_code not in (401, 403)


@pytest.mark.parametrize("path", ["/v1/compliance/diagnose", "/api/compliance/agent/diagnose"])
def test_diagnose_endpoint_returns_mapped_action_items(make_client, bearer, path):
    """Fixed pre-existing crash. Mapping approved by user 2026-10-29: title=summary, description=detailed_findings."""
    res = make_client().get(path, headers=bearer(), params={"jurisdiction": "AE"})
    assert res.status_code == 200
    items = res.json()["action_items"]
    assert items, "the built-in UAE sample batch must produce violations"
    probation = next(i for i in items if i["id"] == "VIO-PROB-EMP-101")
    assert probation["title"] == (
        "Statutory Violation: Probation period of 210 days exceeds statutory maximum of 6 months (180 days)."
    )
    assert "caps probation at 180 days" in probation["description"]
    assert probation["remediation_patch"]["entity_id"] == "EMP-101"
    assert probation["remediation_patch"]["direct_payload"] == {"probation_days": 180}


# AM2 — docs ------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "True", "1", "yes", ""])
def test_docs_disabled_unless_exactly_true(make_client, value):
    client = make_client(env={} if value is None else {"HRMS_ENABLE_API_DOCS": value})
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_docs_enabled_with_true(make_client):
    client = make_client(env={"HRMS_ENABLE_API_DOCS": "true"})
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 200


# §6.8 — CORS ----------------------------------------------------------------


def _preflight(client, origin):
    return client.options(
        "/v1/chat",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )


def test_cors_allowed_origin(make_client):
    res = _preflight(make_client(), "https://hr.acme.test")
    assert res.headers.get("access-control-allow-origin") == "https://hr.acme.test"
    assert res.headers.get("access-control-allow-credentials") != "true"


@pytest.mark.parametrize(
    "origin", ["https://evil.test", "http://hr.acme.test", "https://hr.acme.test:444", "https://hr.initech.test"]
)
def test_cors_disallowed_origins(make_client, origin):
    assert "access-control-allow-origin" not in _preflight(make_client(), origin).headers


def test_cors_port_origin_allowed(make_client):
    res = _preflight(make_client(), "https://hr.initech.test:8443")
    assert res.headers.get("access-control-allow-origin") == "https://hr.initech.test:8443"


# A5 / AM5 — strict jurisdiction ---------------------------------------------------


@pytest.mark.parametrize("value", ["ae", "UAE", "", "XX", "GLOBAL"])
def test_chat_rejects_invalid_jurisdiction(make_client, bearer, value):
    res = make_client().post("/v1/chat", headers=bearer(roles=["staff"]), json={"message": "hi", "jurisdiction": value})
    assert res.status_code == 422


@pytest.mark.parametrize("value", ["ae", "UAE", "", "XX", "GLOBAL"])
def test_voice_turn_rejects_invalid_jurisdiction(make_client, bearer, value):
    res = make_client().post(
        "/v1/voice/turn", headers=bearer(roles=["staff"]), json={"audio_base64": "", "jurisdiction": value}
    )
    assert res.status_code == 422


# §6.6 — remediation body and approver identity ----------------------------------------

REMEDIATION = {"violation_id": "V1", "target_entity": "EMPLOYEE", "entity_id": "EMP-1", "patch_payload": {"a": 1}}


@pytest.mark.parametrize("extra", [{"approver_role": "HR_ADMIN"}, {"approver_id": "MGR"}, {"anything": 1}])
def test_remediation_rejects_unknown_fields(make_client, bearer, extra):
    res = make_client().post("/v1/remediate/execute", headers=bearer(), json={**REMEDIATION, **extra})
    assert res.status_code == 422


def test_remediation_approver_is_token_subject(make_client, bearer):
    res = make_client().post("/v1/remediate/execute", headers=bearer(sub="mgr-42"), json=REMEDIATION)
    assert res.status_code == 200
    trace = res.json()["audit_tracing"]
    assert trace["X-HITL-Approver-Id"] == "mgr-42" and trace["X-Tenant-Id"] == "acme"


def test_handlers_use_token_tenant(make_client, bearer):
    """agy F5: no X-Tenant-Id header and two different tenants, so neither a header-reading nor a
    hard-coded handler can pass."""
    client = make_client()
    acme = client.get("/v1/schema/mappings", headers=bearer(tenant="acme", roles=["platform_admin"]))
    assert acme.status_code == 200 and acme.json()["tenant_id"] == "acme"
    for tenant in ("acme", "globex"):
        headers = bearer(tenant=tenant, roles=["hr_manager"])
        res = client.post("/v1/remediate/execute", json=REMEDIATION, headers=headers)
        assert res.status_code == 200
        assert res.json()["audit_tracing"]["X-Tenant-Id"] == tenant


def test_stream_without_token_is_plain_401(make_client):
    res = make_client().post("/v1/chat/stream", json={"message": "hi"})
    assert res.status_code == 401
    assert not res.headers.get("content-type", "").startswith("text/event-stream")


# AM4 — in-process hostile smoke with the real verifier ----------------------------------------


def test_employee_cannot_execute_remediation(make_client, bearer):
    res = make_client().post("/v1/remediate/execute", headers=bearer(roles=["staff"]), json=REMEDIATION)
    assert res.status_code == 403 and res.json()["permission"] == "EXECUTE_REMEDIATION"
