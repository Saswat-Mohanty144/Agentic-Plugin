"""End-to-End API flow verification tests for Agentic HRMS AI Plugin (AREA: API).

Covers:
1. /healthz public endpoint (positive, boundary, negative methods)
2. Auth & Tenancy on every route (missing/expired/forged tokens, wrong audience, unknown tenant,
   cross-tenant mismatch, RBAC route matrix, cross-tenant isolation)
3. /v1/chat and /v1/chat/stream (ReAct tool loop, SSE framing, in-band budget error, history validation)
4. Per-tenant LLM policy and daily token budget (preflight 429, boundaries, egress audit log rows and statuses)
5. /v1/payroll/pf/calculate (Frappe host mocked, ceilings/boundaries, strict mode, host errors, iCeipts 409)
6. Write-back two-phase flow (/v1/writeback/proposals -> approve -> rollback, separation of duties, freshness,
   non-writable fields, concurrency conflicts, idempotency, iCeipts 409)
7. /v1/remediate/execute (ATTRIBUTION_ONLY, freshness boundary, forbidden extra fields)
8. /v1/hosts/requisitions (Frappe and iCeipts declarative profiles, paging, rejected records, host errors)
"""

from __future__ import annotations

import copy
import json
from typing import Any, Dict, List, Optional

import httpx
import pytest
import yaml

import hrms_plugin.server.app as app_module
from hrms_plugin.agents.supervisor import SupervisorAgent
from hrms_plugin.ai.llm import LLMConfig, LLMGateway, LLMProvider, LLMResponse, ToolCall
from hrms_plugin.governance.llm_policy import LlmPolicy
from hrms_plugin.integrations import declarative
from hrms_plugin.integrations.frappe import FrappeClient
from hrms_plugin.payroll.fingerprint import slip_fingerprint
from hrms_plugin.security.rbac import Permission as P
from tests.support.auth import INITECH_ISSUER, NOW, claims, forge, forge_hs256, mint
from tests.support.scripted_llm import ScriptedLLMClient, final, tool_call
from tests.unit.test_pf_core import RULES_YAML, slip

ICEIPTS_SERVICE_USER = "eb6df26e-e0d3-4d10-8714-873fef6e213c"

HOST_REGISTRY_CONFIG = {
    "tenants": [
        {
            "tenant_id": "acme",
            "vendor": "frappe",
            "base_url": "https://hr.acme.test",
            "api_key_env": "ACME_KEY",
            "api_secret_env": "ACME_SECRET",
            "timeout_seconds": 10,
            "writable_fields": {
                "Employee": {
                    "notice_number_of_days": "int",
                    "final_confirmation_date": "date",
                }
            },
        },
        {
            "tenant_id": "globex",
            "vendor": "iceipts",
            "base_url": "https://api.iceipts.test",
            "token_env": "GLOBEX_TOKEN",
            "service_user_id": ICEIPTS_SERVICE_USER,
            "timeout_seconds": 10,
        },
    ]
}


class MockFrappeServer:
    """Mock Frappe server handling Salary Slip, Employee document, and Job Requisition requests."""

    def __init__(self, slips: Optional[List[Dict[str, Any]]] = None, status: int = 200):
        self.slips = [slip()] if slips is None else slips
        self.status = status
        self.requests: List[httpx.Request] = []
        self.doc = {
            "name": "HR-EMP-00001",
            "employee_name": "Devi Sharma",
            "notice_number_of_days": 30,
            "final_confirmation_date": "2026-03-01",
            "modified": "2026-10-01 10:00:00.000001",
        }
        self.version = 1
        self.puts: List[httpx.Request] = []
        self.fail_put = False
        self.job_requisitions = [
            {
                "name": "HR-HIREQ-00001",
                "designation": "Software Engineer",
                "department": "Engineering",
                "no_of_positions": 3,
                "status": "Open & Approved",
                "description": "Develop core APIs",
                "requested_by": "HR-EMP-00002",
            },
            {
                "name": "HR-HIREQ-00002",
                "designation": "Product Manager",
                "department": "Product",
                "no_of_positions": 1,
                "status": "Pending",
                "description": "Lead product roadmap",
                "requested_by": "HR-EMP-00003",
            },
        ]

    def touch(self) -> None:
        self.version += 1
        self.doc["modified"] = f"2026-10-01 10:00:00.{self.version:06d}"

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"exc": "upstream host error"})

        path = request.url.path
        if path == "/api/resource/Salary Slip":
            return httpx.Response(200, json={"data": [{"name": s["name"]} for s in self.slips]})
        if path.startswith("/api/resource/Salary Slip/"):
            return httpx.Response(200, json={"data": self.slips[0]})

        if path.startswith("/api/resource/Employee/"):
            if request.method == "GET":
                return httpx.Response(200, json={"data": copy.deepcopy(self.doc)})
            if request.method == "PUT":
                self.puts.append(request)
                if self.fail_put:
                    return httpx.Response(500, json={"exc": "PUT failed"})
                patch = json.loads(request.content)
                self.touch()
                self.doc.update(patch)
                return httpx.Response(200, json={"data": copy.deepcopy(self.doc)})

        if path == "/api/resource/Job Requisition":
            return httpx.Response(200, json={"data": copy.deepcopy(self.job_requisitions)})

        return httpx.Response(404, json={"message": "Not Found"})


class MockIceiptsServer:
    """Mock iCeipts server handling Requisition pagination."""

    def __init__(self, pages: Optional[List[Dict[str, Any]]] = None, status: int = 200):
        self.pages = pages or []
        self.status = status
        self.requests: List[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "iCeipts failure"})
        page_idx = int(request.url.params.get("page", 1)) - 1
        if 0 <= page_idx < len(self.pages):
            return httpx.Response(200, json=self.pages[page_idx])
        return httpx.Response(
            200,
            json={
                "success": True,
                "status": 200,
                "data": {"pageData": [], "totalPages": 0, "currentPage": 1, "totalrows": 0},
            },
        )


@pytest.fixture
def host_environments(tmp_path, monkeypatch):
    """Configures HRMS_HOSTS, HRMS_PAYROLL_RULES, and host credentials."""
    hosts_file = tmp_path / "hosts.yaml"
    hosts_file.write_text(yaml.safe_dump(HOST_REGISTRY_CONFIG), encoding="utf-8")
    rules_file = tmp_path / "payroll_rules.yaml"
    rules_file.write_text(yaml.safe_dump(RULES_YAML), encoding="utf-8")

    monkeypatch.setenv("HRMS_HOSTS", str(hosts_file))
    monkeypatch.setenv("HRMS_PAYROLL_RULES", str(rules_file))
    monkeypatch.setenv("ACME_KEY", "acme-api-key")
    monkeypatch.setenv("ACME_SECRET", "acme-api-secret")
    monkeypatch.setenv("GLOBEX_TOKEN", "mock-globex-bearer-jwt")

    app_module.writeback_service._clock = lambda: NOW
    app_module.payroll_service._clock = lambda: NOW
    app_module.egress_log._clock = lambda: NOW
    return monkeypatch


@pytest.fixture
def frappe_mock(host_environments, monkeypatch):
    server = MockFrappeServer()

    def _factory(h, k, s):
        return FrappeClient(
            h, k, s, http_client=httpx.AsyncClient(transport=httpx.MockTransport(server.handler))
        )

    monkeypatch.setattr(app_module.writeback_service, "_client_factory", _factory)
    monkeypatch.setattr(app_module.payroll_service, "_client_factory", _factory)
    monkeypatch.setattr(declarative, "_default_transport", lambda: httpx.MockTransport(server.handler))
    return server


# ==============================================================================
# SECTION 1: /healthz Public Route
# ==============================================================================


def test_e2e_healthz_positive_unauthenticated(make_client):
    """(1) Positive: /healthz is public and returns 200 with service health info."""
    client = make_client()
    resp = client.get("/healthz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert data["version"] == "2.0.0"
    assert "IN" in data["jurisdictions_supported"]


def test_e2e_healthz_boundary_query_params_ignored(make_client):
    """(2) Boundary: /healthz with query strings still passes without error."""
    client = make_client()
    resp = client.get("/healthz?probe=readiness&timeout=30")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


@pytest.mark.parametrize("method", ["post", "put", "delete", "patch"])
def test_e2e_healthz_negative_method_not_allowed(make_client, method):
    """(3) Negative: unsupported HTTP methods return 405 Method Not Allowed."""
    client = make_client()
    call = getattr(client, method)
    resp = call("/healthz")
    assert resp.status_code == 405


# ==============================================================================
# SECTION 2: Auth & Tenancy on Every Route & RBAC Matrix
# ==============================================================================


def test_e2e_auth_missing_token_returns_401_challenge(make_client):
    """(3) Negative: Protected route with missing token returns 401 with challenge."""
    client = make_client()
    resp = client.post("/v1/chat", json={"message": "hello", "jurisdiction": "IN"})
    assert resp.status_code == 401
    assert resp.json() == {"error": "UNAUTHENTICATED"}
    assert resp.headers["www-authenticate"] == 'Bearer error="invalid_token"'


def test_e2e_auth_expired_token_returns_401(make_client, bearer):
    """(3) Negative: Token expired past leeway returns 401 UNAUTHENTICATED."""
    client = make_client()
    headers = bearer(iat=NOW - 1000, lifetime=600)
    resp = client.post("/v1/chat", json={"message": "hello", "jurisdiction": "IN"}, headers=headers)
    assert resp.status_code == 401
    assert resp.json() == {"error": "UNAUTHENTICATED"}


def test_e2e_auth_forged_signature_returns_401(make_client, keys):
    """(3) Negative: Token signed with unauthorized private key returns 401."""
    client = make_client()
    forged = mint(keys.initech_rsa, "acme-rsa", claims())
    resp = client.post(
        "/v1/chat",
        json={"message": "hello", "jurisdiction": "IN"},
        headers={"Authorization": f"Bearer {forged}"},
    )
    assert resp.status_code == 401
    assert resp.json() == {"error": "UNAUTHENTICATED"}


def test_e2e_auth_wrong_audience_returns_401(make_client, bearer):
    """(3) Negative: Token minted for foreign audience returns 401."""
    client = make_client()
    headers = bearer(audience="other-audience")
    resp = client.post("/v1/chat", json={"message": "hello", "jurisdiction": "IN"}, headers=headers)
    assert resp.status_code == 401
    assert resp.json() == {"error": "UNAUTHENTICATED"}


def test_e2e_auth_unknown_tenant_returns_401(make_client, bearer):
    """(3) Negative: Token naming an unregistered tenant returns 401."""
    client = make_client()
    headers = bearer(tenant="nonexistent-corp")
    resp = client.post("/v1/chat", json={"message": "hello", "jurisdiction": "IN"}, headers=headers)
    assert resp.status_code == 401
    assert resp.json() == {"error": "UNAUTHENTICATED"}


def test_e2e_auth_alg_none_returns_401(make_client):
    """(3) Negative: Unsigned token with alg=none returns 401 UNAUTHENTICATED."""
    client = make_client()
    unsigned = forge({"alg": "none", "typ": "JWT"}, claims())
    resp = client.post(
        "/v1/chat",
        json={"message": "hello", "jurisdiction": "IN"},
        headers={"Authorization": f"Bearer {unsigned}"},
    )
    assert resp.status_code == 401
    assert resp.json() == {"error": "UNAUTHENTICATED"}


def test_e2e_auth_alg_hs256_confusion_returns_401(make_client):
    """(3) Negative: Symmetric HS256 algorithm confusion attack returns 401 UNAUTHENTICATED."""
    client = make_client()
    forged = forge_hs256(claims(), b"symmetric-secret", "acme-rsa")
    resp = client.post(
        "/v1/chat",
        json={"message": "hello", "jurisdiction": "IN"},
        headers={"Authorization": f"Bearer {forged}"},
    )
    assert resp.status_code == 401
    assert resp.json() == {"error": "UNAUTHENTICATED"}


def test_e2e_auth_tenant_issuer_mismatch_returns_401(make_client, keys):
    """(3) Negative: Token for tenant B minted under tenant A issuer returns 401 UNAUTHENTICATED."""
    client = make_client()
    # Initech tenant under Acme issuer
    mismatched = mint(keys.rsa, "acme-rsa", claims(tenant="initech", issuer=INITECH_ISSUER))
    resp = client.post(
        "/v1/chat",
        json={"message": "hello", "jurisdiction": "IN"},
        headers={"Authorization": f"Bearer {mismatched}"},
    )
    assert resp.status_code == 401
    assert resp.json() == {"error": "UNAUTHENTICATED"}


def test_e2e_auth_cross_tenant_header_mismatch_403(make_client, bearer):
    """(3) Negative: Token for acme with X-Tenant-Id globex returns 403 TENANT_MISMATCH."""
    client = make_client()
    headers = {**bearer(tenant="acme"), "X-Tenant-Id": "globex"}
    resp = client.post("/v1/chat", json={"message": "hello", "jurisdiction": "IN"}, headers=headers)
    assert resp.status_code == 403
    assert resp.json() == {"error": "TENANT_MISMATCH"}


def test_e2e_auth_matching_tenant_header_accepted(make_client, bearer):
    """(1) Positive / (2) Boundary: Token for acme with matching X-Tenant-Id acme is accepted."""
    client = make_client()
    headers = {**bearer(tenant="acme", roles=["hr_manager"]), "X-Tenant-Id": "acme"}
    resp = client.post("/v1/chat", json={"message": "hello", "jurisdiction": "IN"}, headers=headers)
    assert resp.status_code not in (401, 403)


def test_e2e_auth_case_insensitive_bearer_accepted(make_client, make_token):
    """(2) Boundary: Scheme 'bearer' (all lowercase) is accepted."""
    client = make_client()
    token = make_token(roles=["hr_manager"])
    headers = {"Authorization": f"bearer {token}"}
    resp = client.post("/v1/chat", json={"message": "hello", "jurisdiction": "IN"}, headers=headers)
    assert resp.status_code not in (401, 403)


def test_e2e_auth_duplicate_authorization_headers_401(make_client, make_token):
    """(3) Negative: Duplicate Authorization headers fail closed with 401."""
    client = make_client()
    t = make_token()
    headers = [("Authorization", f"Bearer {t}"), ("Authorization", f"Bearer {t}")]
    resp = client.post("/v1/chat", json={"message": "hello", "jurisdiction": "IN"}, headers=headers)
    assert resp.status_code == 401


# RBAC Route Matrix checks
ROUTE_PERM_MAP = {
    ("POST", "/v1/chat"): (P.USE_COPILOT, "janitor", "hr_manager"),
    ("POST", "/v1/chat/stream"): (P.USE_COPILOT, "janitor", "hr_manager"),
    ("POST", "/v1/schema/introspect"): (P.MANAGE_SCHEMA, "hr_manager", "platform_admin"),
    ("GET", "/v1/schema/mappings"): (P.MANAGE_SCHEMA, "hr_manager", "platform_admin"),
    ("POST", "/v1/remediate/execute"): (P.EXECUTE_REMEDIATION, "staff", "hr_manager"),
    ("POST", "/v1/writeback/proposals"): (P.MUTATE_EMPLOYEE, "payroll_officer", "hr_manager"),
    ("POST", "/v1/payroll/pf/calculate"): (P.VIEW_SALARY, "staff", "payroll_officer"),
    ("GET", "/v1/hosts/requisitions"): (P.MANAGE_RECRUITMENT, "staff", "recruiter"),
    ("GET", "/v1/governance/llm-egress"): (P.VIEW_COMPLIANCE, "staff", "hr_manager"),
}


@pytest.mark.parametrize("method,path", sorted(ROUTE_PERM_MAP.keys()))
def test_e2e_rbac_route_matrix_unauthorized_role_forbidden(make_client, bearer, method, path):
    """(3) Negative: Caller lacking required permission receives 403 with permission name."""
    perm, lacking_role, _ = ROUTE_PERM_MAP[(method, path)]
    client = make_client()
    call = getattr(client, method.lower())
    kw = {"json": {}} if method == "POST" else {}
    resp = call(path, headers=bearer(roles=[lacking_role]), **kw)
    assert resp.status_code == 403
    assert resp.json() == {"error": "FORBIDDEN", "permission": perm.value}


@pytest.mark.parametrize("method,path", sorted(ROUTE_PERM_MAP.keys()))
def test_e2e_rbac_route_matrix_authorized_role_passes(make_client, bearer, method, path):
    """(1) Positive: Caller holding required permission passes authentication and authorization."""
    _, _, holding_role = ROUTE_PERM_MAP[(method, path)]
    client = make_client(raise_server_exceptions=False)
    call = getattr(client, method.lower())
    kw = {"json": {}} if method == "POST" else {}
    resp = call(path, headers=bearer(roles=[holding_role]), **kw)
    assert resp.status_code not in (401, 403)


# ==============================================================================
# SECTION 3: /v1/chat and /v1/chat/stream Endpoints
# ==============================================================================


def test_e2e_chat_react_tool_loop_full_cycle(make_client, bearer, scripted):
    """(1) Positive: /v1/chat ReAct loop: tool call -> tool observation -> final synthesis."""
    gateway, _ = scripted(
        tool_call("calculate_salary_structure", basic_salary=10000.0),
        final("The salary structure has been computed with basic salary 10,000 INR."),
    )
    client = make_client(supervisor_agent=SupervisorAgent(llm_gateway=gateway))
    payload = {
        "message": "Calculate salary structure for basic 10000",
        "jurisdiction": "IN",
        "agentic": True,
    }
    resp = client.post("/v1/chat", json=payload, headers=bearer(tenant="acme", roles=["hr_manager"]))
    assert resp.status_code == 200
    data = resp.json()
    assert "reply_text" in data
    assert "salary structure" in data["reply_text"].lower()
    assert data["intent"] == "PAYROLL_STRUCTURE"
    assert len(data.get("tool_calls", [])) > 0
    assert data["tool_calls"][0]["name"] == "calculate_salary_structure"


def test_e2e_chat_stream_sse_framing_and_event_progression(make_client, bearer, scripted):
    """(1) Positive / (2) Boundary: /v1/chat/stream SSE framing and event sequencing."""
    gateway, _ = scripted(
        tool_call("calculate_salary_structure", basic_salary=8000.0),
        final("Salary breakdown finalized."),
    )
    client = make_client(supervisor_agent=SupervisorAgent(llm_gateway=gateway))
    payload = {
        "message": "Stream breakdown for basic 8000",
        "jurisdiction": "IN",
        "agentic": True,
    }
    resp = client.post("/v1/chat/stream", json=payload, headers=bearer(tenant="acme", roles=["hr_manager"]))
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]

    events = [json.loads(line[6:]) for line in resp.text.splitlines() if line.startswith("data: ")]
    event_names = [e["event"] for e in events]

    assert "thinking" in event_names
    assert "tool_call" in event_names
    assert "tool_observation" in event_names
    assert "token" in event_names
    assert "done" in event_names
    assert "intent" in event_names

    tool_call_ev = next(e for e in events if e["event"] == "tool_call")
    assert tool_call_ev["data"]["name"] == "calculate_salary_structure"


def test_e2e_chat_stream_no_tool_single_turn_streaming(make_client, bearer, scripted):
    """(1) Positive: /v1/chat/stream single-turn response streams tokens without tool invocations."""
    gateway, _ = scripted(final("Labor regulations require statutory records to be kept."))
    client = make_client(supervisor_agent=SupervisorAgent(llm_gateway=gateway))
    payload = {
        "message": "What is the general notice requirement?",
        "jurisdiction": "IN",
        "agentic": True,
    }
    resp = client.post("/v1/chat/stream", json=payload, headers=bearer(tenant="acme", roles=["hr_manager"]))
    assert resp.status_code == 200
    events = [json.loads(line[6:]) for line in resp.text.splitlines() if line.startswith("data: ")]
    event_names = [e["event"] for e in events]
    assert "tool_call" not in event_names
    assert "token" in event_names
    assert events[-1]["event"] == "intent"


def test_e2e_chat_stream_in_band_budget_exhaustion(make_client, bearer):
    """(3) Negative: When budget exhausts between ReAct turns, SSE yields in-band error."""
    tool_resp = LLMResponse(
        content="",
        tool_calls=[ToolCall(id="c-budget", name="search_labor_regulations", arguments={"query": "gratuity"})],
        finish_reason="tool_use",
        model="scripted",
        provider="scripted",
        usage={"input_tokens": 50, "output_tokens": 50},
    )
    client_llm = ScriptedLLMClient([tool_resp])
    gw = LLMGateway(config=LLMConfig(provider=LLMProvider.FAKE))
    gw.client = client_llm
    gw.egress_log = app_module.egress_log
    app_module.egress_log._clock = lambda: NOW
    # First turn uses 100 tokens which reaches the budget of 100
    gw.policy_resolver = lambda tid, prov: LlmPolicy(
            allowed_providers=("fake",), max_data_tier=2, daily_token_budget=100)

    # Ensure daily usage starts at 0 for acme
    day_key = app_module.egress_log._day_key("acme", NOW)
    app_module._storage.set("llm_egress_daily", day_key, {"tokens": 0})

    client = make_client(supervisor_agent=SupervisorAgent(llm_gateway=gw))
    resp = client.post(
        "/v1/chat/stream",
        json={"message": "Check gratuity regulation", "jurisdiction": "IN"},
        headers=bearer(tenant="acme", roles=["hr_manager"]),
    )
    assert resp.status_code == 200
    events = [json.loads(line[6:]) for line in resp.text.splitlines() if line.startswith("data: ")]
    assert events[-1] == {"event": "error", "data": {"error": "LLM_BUDGET_EXHAUSTED"}}


def test_e2e_chat_history_positive_flow(make_client, bearer, scripted):
    """(1) Positive: Conversational history is passed cleanly to the LLM."""
    gateway, client_llm = scripted(final("Acknowledged previous turns."))
    client = make_client(supervisor_agent=SupervisorAgent(llm_gateway=gateway))
    history = [
        {"role": "user", "content": "My name is John Doe"},
        {"role": "assistant", "content": "Hello John"},
    ]
    resp = client.post(
        "/v1/chat",
        json={"message": "What is my statutory benefit?", "jurisdiction": "IN", "history": history},
        headers=bearer(tenant="acme", roles=["hr_manager"]),
    )
    assert resp.status_code == 200
    sent_msgs = [m.content for m in client_llm.calls[0]["messages"]]
    assert "My name is John Doe" in sent_msgs


def test_e2e_chat_history_boundary_empty_list(make_client, bearer, scripted):
    """(2) Boundary: Empty history list [] is accepted."""
    gateway, _ = scripted(final("OK"))
    client = make_client(supervisor_agent=SupervisorAgent(llm_gateway=gateway))
    resp = client.post(
        "/v1/chat",
        json={"message": "Hi", "jurisdiction": "IN", "history": []},
        headers=bearer(tenant="acme", roles=["hr_manager"]),
    )
    assert resp.status_code == 200


def test_e2e_chat_history_boundary_exact_max_50_items(make_client, bearer, scripted):
    """(2) Boundary: Exactly 50 history turns is accepted."""
    gateway, _ = scripted(final("OK 50"))
    client = make_client(supervisor_agent=SupervisorAgent(llm_gateway=gateway))
    history = []
    for i in range(25):
        history.append({"role": "user", "content": f"query {i}"})
        history.append({"role": "assistant", "content": f"response {i}"})

    assert len(history) == 50
    resp = client.post(
        "/v1/chat",
        json={"message": "Next query", "jurisdiction": "IN", "history": history},
        headers=bearer(tenant="acme", roles=["hr_manager"]),
    )
    assert resp.status_code == 200


def test_e2e_chat_history_boundary_exact_max_32000_chars(make_client, bearer, scripted):
    """(2) Boundary: History item content of exactly 32,000 characters is accepted."""
    gateway, _ = scripted(final("OK 32000 chars"))
    client = make_client(supervisor_agent=SupervisorAgent(llm_gateway=gateway))
    large_turn = [{"role": "user", "content": "A" * 32000}]
    resp = client.post(
        "/v1/chat",
        json={"message": "Hi", "jurisdiction": "IN", "history": large_turn},
        headers=bearer(tenant="acme", roles=["hr_manager"]),
    )
    assert resp.status_code == 200


def test_e2e_chat_history_boundary_multilingual_and_emoji(make_client, bearer, scripted):
    """(2) Boundary: Unicode content (Hindi, Arabic, Japanese, Emoji) is accepted and preserved."""
    gateway, client_llm = scripted(final("Multilingual acknowledged."))
    client = make_client(supervisor_agent=SupervisorAgent(llm_gateway=gateway))
    multilingual = "नमस्ते مرحباً こんにちは 🚀 🌟"
    history = [
        {"role": "user", "content": multilingual},
        {"role": "assistant", "content": "उत्तर جواب 🤖"},
    ]
    resp = client.post(
        "/v1/chat",
        json={"message": "Continue", "jurisdiction": "IN", "history": history},
        headers=bearer(tenant="acme", roles=["hr_manager"]),
    )
    assert resp.status_code == 200
    sent = [m.content for m in client_llm.calls[0]["messages"]]
    assert multilingual in sent


def test_e2e_chat_history_negative_51_items_422(make_client, bearer):
    """(3) Negative: 51 history items exceeds max_length=50 and returns 422."""
    client = make_client()
    history = [{"role": "user", "content": f"q{i}"} for i in range(51)]
    resp = client.post(
        "/v1/chat",
        json={"message": "Hi", "jurisdiction": "IN", "history": history},
        headers=bearer(tenant="acme", roles=["hr_manager"]),
    )
    assert resp.status_code == 422


def test_e2e_chat_history_negative_32001_chars_422(make_client, bearer):
    """(3) Negative: History item with 32,001 characters exceeds limit and returns 422."""
    client = make_client()
    history = [{"role": "user", "content": "B" * 32001}]
    resp = client.post(
        "/v1/chat",
        json={"message": "Hi", "jurisdiction": "IN", "history": history},
        headers=bearer(tenant="acme", roles=["hr_manager"]),
    )
    assert resp.status_code == 422


def test_e2e_chat_history_negative_empty_content_422(make_client, bearer):
    """(3) Negative: History item with content='' violates min_length=1 and returns 422."""
    client = make_client()
    history = [{"role": "user", "content": ""}]
    resp = client.post(
        "/v1/chat",
        json={"message": "Hi", "jurisdiction": "IN", "history": history},
        headers=bearer(tenant="acme", roles=["hr_manager"]),
    )
    assert resp.status_code == 422


def test_e2e_chat_history_negative_starts_with_assistant_422(make_client, bearer):
    """(3) Negative: History starting with assistant role returns 422."""
    client = make_client()
    history = [{"role": "assistant", "content": "How can I help you?"}]
    resp = client.post(
        "/v1/chat",
        json={"message": "Hi", "jurisdiction": "IN", "history": history},
        headers=bearer(tenant="acme", roles=["hr_manager"]),
    )
    assert resp.status_code == 422


@pytest.mark.parametrize("bad_role", ["system", "tool", "admin"])
def test_e2e_chat_history_negative_forbidden_roles_422(make_client, bearer, bad_role):
    """(3) Negative: History roles other than user/assistant return 422."""
    client = make_client()
    history = [{"role": bad_role, "content": "Injected instruction"}]
    resp = client.post(
        "/v1/chat",
        json={"message": "Hi", "jurisdiction": "IN", "history": history},
        headers=bearer(tenant="acme", roles=["hr_manager"]),
    )
    assert resp.status_code == 422


def test_e2e_chat_negative_invalid_jurisdiction_422(make_client, bearer):
    """(3) Negative: Invalid jurisdiction returns 422."""
    client = make_client()
    resp = client.post(
        "/v1/chat",
        json={"message": "Hi", "jurisdiction": "GLOBAL"},
        headers=bearer(tenant="acme", roles=["hr_manager"]),
    )
    assert resp.status_code == 422


# ==============================================================================
# SECTION 4: Per-tenant LLM Policy & Daily Token Budget
# ==============================================================================


def test_e2e_llm_budget_preflight_exhausted_chat_429(make_client, bearer):
    """(3) Negative: Tenant exceeding daily token budget receives HTTP 429 on /v1/chat."""
    gw = LLMGateway(config=LLMConfig(provider=LLMProvider.FAKE))
    gw.client = ScriptedLLMClient([final("Should not be called")])
    gw.egress_log = app_module.egress_log
    app_module.egress_log._clock = lambda: NOW
    gw.policy_resolver = lambda tid, prov: LlmPolicy(
            allowed_providers=("fake",), max_data_tier=2, daily_token_budget=100)

    # Exhaust budget in daily counter
    day_key = app_module.egress_log._day_key("acme", NOW)
    app_module._storage.set("llm_egress_daily", day_key, {"tokens": 100})

    client = make_client(supervisor_agent=SupervisorAgent(llm_gateway=gw))
    resp = client.post(
        "/v1/chat",
        json={"message": "Calculate compliance", "jurisdiction": "IN"},
        headers=bearer(tenant="acme", roles=["hr_manager"]),
    )
    assert resp.status_code == 429
    assert resp.json() == {"error": "LLM_BUDGET_EXHAUSTED"}


def test_e2e_llm_budget_exact_limits_boundary(make_client, bearer):
    """(2) Boundary: tokens_today == budget - 1 passes, tokens_today == budget fails 429."""
    gw = LLMGateway(config=LLMConfig(provider=LLMProvider.FAKE))
    client_llm = ScriptedLLMClient([final("Turn allowed")])
    gw.client = client_llm
    gw.egress_log = app_module.egress_log
    app_module.egress_log._clock = lambda: NOW
    gw.policy_resolver = lambda tid, prov: LlmPolicy(
            allowed_providers=("fake",), max_data_tier=2, daily_token_budget=100)

    # Set tokens_today to exactly 99 (budget - 1)
    day_key = app_module.egress_log._day_key("acme", NOW)
    app_module._storage.set("llm_egress_daily", day_key, {"tokens": 99})

    client = make_client(supervisor_agent=SupervisorAgent(llm_gateway=gw))
    resp1 = client.post(
        "/v1/chat",
        json={"message": "Query at 99 tokens", "jurisdiction": "IN"},
        headers=bearer(tenant="acme", roles=["hr_manager"]),
    )
    assert resp1.status_code == 200

    # Advance to exactly 100 tokens (equal to budget)
    app_module._storage.set("llm_egress_daily", day_key, {"tokens": 100})
    resp2 = client.post(
        "/v1/chat",
        json={"message": "Query at 100 tokens", "jurisdiction": "IN"},
        headers=bearer(tenant="acme", roles=["hr_manager"]),
    )
    assert resp2.status_code == 429
    assert resp2.json() == {"error": "LLM_BUDGET_EXHAUSTED"}


def test_e2e_llm_policy_provider_forbidden_403(make_client, bearer):
    """(3) Negative: Tenant whose policy disallows configured LLM provider returns 403."""
    from hrms_plugin.governance.llm_policy import LlmNotAllowed

    gw = LLMGateway(config=LLMConfig(provider=LLMProvider.FAKE))

    def forbidden_resolver(tid, prov):
        raise LlmNotAllowed(f"provider {prov} is forbidden for tenant {tid}")

    gw.policy_resolver = forbidden_resolver

    client = make_client(supervisor_agent=SupervisorAgent(llm_gateway=gw))
    resp = client.post(
        "/v1/chat",
        json={"message": "Hello", "jurisdiction": "IN"},
        headers=bearer(tenant="acme", roles=["hr_manager"]),
    )
    assert resp.status_code == 403
    assert resp.json() == {"error": "LLM_NOT_ALLOWED_FOR_TENANT"}


def test_e2e_governance_llm_egress_lifecycle_and_statuses(make_client, bearer):
    """(1) Positive / (2) Boundary: GET /v1/governance/llm-egress reports content-free records with statuses."""
    client = make_client()
    for status in ("COMPLETED", "SENT", "ABORTED", "FAILED"):
        app_module.egress_log.record(
            tenant_id="acme",
            provider="fake",
            model="test-model",
            payload_sha256="sha256:" + "a" * 64,
            redacted_kinds={"NAME": 1},
            input_tokens=10,
            output_tokens=20,
            status=status,
        )

    resp = client.get("/v1/governance/llm-egress", headers=bearer(tenant="acme", roles=["hr_manager"]))
    assert resp.status_code == 200
    data = resp.json()
    assert data["tenant_id"] == "acme"
    statuses_seen = {r["status"] for r in data["records"]}
    assert {"COMPLETED", "SENT", "ABORTED", "FAILED"} <= statuses_seen

    # Verify content-free invariant (no prompt text, no secret, no PII)
    for record in data["records"]:
        assert "prompt" not in record
        assert "content" not in record
        assert "response" not in record
        assert record["payload_sha256"].startswith("sha256:")


def test_e2e_governance_llm_egress_tenant_isolation(make_client, bearer):
    """(1) Positive / (2) Boundary: Tenant isolation on LLM egress log."""
    app_module.egress_log.record(
        tenant_id="acme",
        provider="fake",
        model="m",
        payload_sha256="sha256:" + "1" * 64,
        redacted_kinds={},
        status="COMPLETED",
    )
    app_module.egress_log.record(
        tenant_id="globex",
        provider="fake",
        model="m",
        payload_sha256="sha256:" + "2" * 64,
        redacted_kinds={},
        status="COMPLETED",
    )

    client = make_client()
    resp_acme = client.get("/v1/governance/llm-egress", headers=bearer(tenant="acme", roles=["hr_manager"]))
    assert resp_acme.status_code == 200
    assert all(r["tenant_id"] == "acme" for r in resp_acme.json()["records"])

    resp_globex = client.get("/v1/governance/llm-egress", headers=bearer(tenant="globex", roles=["hr_manager"]))
    assert resp_globex.status_code == 200
    assert all(r["tenant_id"] == "globex" for r in resp_globex.json()["records"])


# ==============================================================================
# SECTION 5: /v1/payroll/pf/calculate Endpoint
# ==============================================================================


def test_e2e_pf_calculate_positive_happy_path(make_client, bearer, frappe_mock):
    """(1) Positive: /v1/payroll/pf/calculate returns deterministic EPF/EPS amounts."""
    client = make_client()
    fp = slip_fingerprint(frappe_mock.slips[0])
    app_module.payroll_service.approve_fingerprint("acme", "frappe", "Salary Slip", fp, "approver-1")

    payload = {"employee_id": "HR-EMP-00001", "period": "2026-09"}
    resp = client.post(
        "/v1/payroll/pf/calculate",
        json=payload,
        headers=bearer(tenant="acme", roles=["payroll_officer"]),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["pf_wage"] == "14000"  # Basic 12000 + DA 2000
    assert data["employee_contribution"] == "1680"  # 12% of 14000
    assert data["employer_eps"] == "1166"  # 8.33% of 14000
    assert data["employer_epf"] == "514"  # 1680 - 1166
    assert data["employer_total"] == "1680"
    assert data["provenance"]["rules_version"] == "2026-10-acme-1"


@pytest.mark.parametrize(
    "basic,da,expected_wage,expected_ee,expected_eps,expected_er_epf",
    [
        (10000, 2000, "12000", "1440", "1000", "440"),  # below ceiling
        (12000, 3000, "15000", "1800", "1250", "550"),  # exact ceiling
        (18000, 4000, "22000", "1800", "1250", "550"),  # above ceiling (capped)
    ],
)
def test_e2e_pf_calculate_boundaries_ceilings(
    make_client, bearer, frappe_mock, basic, da, expected_wage, expected_ee, expected_eps, expected_er_epf
):
    """(2) Boundary: PF wage below, at, and above statutory ₹15,000 ceiling."""
    frappe_mock.slips = [slip(basic=basic, da=da)]
    fp = slip_fingerprint(frappe_mock.slips[0])
    app_module.payroll_service.approve_fingerprint("acme", "frappe", "Salary Slip", fp, "approver-1")

    client = make_client()
    resp = client.post(
        "/v1/payroll/pf/calculate",
        json={"employee_id": "HR-EMP-00001", "period": "2026-09"},
        headers=bearer(tenant="acme", roles=["payroll_officer"]),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["pf_wage"] == expected_wage
    assert data["employee_contribution"] == expected_ee
    assert data["employer_eps"] == expected_eps
    assert data["employer_epf"] == expected_er_epf


def test_e2e_pf_calculate_strict_mode_gross_mismatch_502(make_client, bearer, frappe_mock):
    """(3) Negative: Slip with sum(earnings) != gross_pay fails closed with 502 INCONSISTENT_PAYLOAD."""
    bad_slip = slip(gross=99999)  # earnings sum to 20000 != 99999
    frappe_mock.slips = [bad_slip]
    fp = slip_fingerprint(bad_slip)
    app_module.payroll_service.approve_fingerprint("acme", "frappe", "Salary Slip", fp, "approver-1")

    client = make_client()
    resp = client.post(
        "/v1/payroll/pf/calculate",
        json={"employee_id": "HR-EMP-00001", "period": "2026-09"},
        headers=bearer(tenant="acme", roles=["payroll_officer"]),
    )
    assert resp.status_code == 502
    assert resp.json()["error"] == "INCONSISTENT_PAYLOAD"


def test_e2e_pf_calculate_strict_mode_currency_mismatch_502(make_client, bearer, frappe_mock):
    """(3) Negative: Slip currency != rule currency fails closed with 502 INCONSISTENT_PAYLOAD."""
    bad_slip = slip(currency="USD")
    frappe_mock.slips = [bad_slip]
    fp = slip_fingerprint(bad_slip)
    app_module.payroll_service.approve_fingerprint("acme", "frappe", "Salary Slip", fp, "approver-1")

    client = make_client()
    resp = client.post(
        "/v1/payroll/pf/calculate",
        json={"employee_id": "HR-EMP-00001", "period": "2026-09"},
        headers=bearer(tenant="acme", roles=["payroll_officer"]),
    )
    assert resp.status_code == 502
    assert resp.json()["error"] == "INCONSISTENT_PAYLOAD"


def test_e2e_pf_calculate_strict_mode_missing_required_component_409(make_client, bearer, frappe_mock):
    """(3) Negative: Slip missing required component 'B' returns 409 PF_COMPONENT_MISSING."""
    bad_slip = slip()
    # Remove 'B' component
    bad_slip["earnings"] = [e for e in bad_slip["earnings"] if e["abbr"] != "B"]
    bad_slip["gross_pay"] = sum(e["amount"] for e in bad_slip["earnings"])
    frappe_mock.slips = [bad_slip]
    fp = slip_fingerprint(bad_slip)
    app_module.payroll_service.approve_fingerprint("acme", "frappe", "Salary Slip", fp, "approver-1")

    client = make_client()
    resp = client.post(
        "/v1/payroll/pf/calculate",
        json={"employee_id": "HR-EMP-00001", "period": "2026-09"},
        headers=bearer(tenant="acme", roles=["payroll_officer"]),
    )
    assert resp.status_code == 409
    assert resp.json()["error"] == "PF_COMPONENT_MISSING"


def test_e2e_pf_calculate_schema_fingerprint_unapproved_409(make_client, bearer, frappe_mock):
    """(3) Negative: Unapproved schema fingerprint returns 409 MAPPING_NOT_APPROVED."""
    # Ensure no approved fingerprint exists in storage
    app_module._storage.clear("approved_fingerprints")
    client = make_client()
    resp = client.post(
        "/v1/payroll/pf/calculate",
        json={"employee_id": "HR-EMP-00001", "period": "2026-09"},
        headers=bearer(tenant="acme", roles=["payroll_officer"]),
    )
    assert resp.status_code == 409
    data = resp.json()
    assert data["error"] == "MAPPING_NOT_APPROVED"
    assert "observed_fingerprint" in data


def test_e2e_pf_calculate_schema_fingerprint_drift_409(make_client, bearer, frappe_mock):
    """(3) Negative: Schema fingerprint drift returns 409 SCHEMA_DRIFT and quarantines document."""
    app_module.payroll_service.approve_fingerprint(
        "acme", "frappe", "Salary Slip", "sha256:" + "0" * 64, "approver-1"
    )
    client = make_client()
    resp = client.post(
        "/v1/payroll/pf/calculate",
        json={"employee_id": "HR-EMP-00001", "period": "2026-09"},
        headers=bearer(tenant="acme", roles=["payroll_officer"]),
    )
    assert resp.status_code == 409
    assert resp.json()["error"] == "SCHEMA_DRIFT"

    # Check quarantine listing
    q_resp = client.get(
        "/v1/schema/fingerprints/quarantine",
        headers=bearer(tenant="acme", roles=["platform_admin"]),
    )
    assert q_resp.status_code == 200
    quarantine = q_resp.json()["quarantine"]
    assert any(q["document"] == frappe_mock.slips[0]["name"] for q in quarantine)


def test_e2e_pf_calculate_host_error_0_slips_404(make_client, bearer, frappe_mock):
    """(3) Negative: 0 submitted slips returns 404 SLIP_NOT_FOUND."""
    frappe_mock.slips = []
    client = make_client()
    resp = client.post(
        "/v1/payroll/pf/calculate",
        json={"employee_id": "HR-EMP-00001", "period": "2026-09"},
        headers=bearer(tenant="acme", roles=["payroll_officer"]),
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "SLIP_NOT_FOUND"


def test_e2e_pf_calculate_host_error_ambiguous_slips_409(make_client, bearer, frappe_mock):
    """(3) Negative: Multiple slips in the same period returns 409 AMBIGUOUS_SLIPS."""
    frappe_mock.slips = [slip(), slip()]
    client = make_client()
    resp = client.post(
        "/v1/payroll/pf/calculate",
        json={"employee_id": "HR-EMP-00001", "period": "2026-09"},
        headers=bearer(tenant="acme", roles=["payroll_officer"]),
    )
    assert resp.status_code == 409
    assert resp.json()["error"] == "AMBIGUOUS_SLIPS"


def test_e2e_pf_calculate_host_upstream_500_error_502(make_client, bearer, frappe_mock):
    """(3) Negative: Upstream host 500 error maps to 502 HOST_ERROR."""
    frappe_mock.status = 500
    client = make_client()
    resp = client.post(
        "/v1/payroll/pf/calculate",
        json={"employee_id": "HR-EMP-00001", "period": "2026-09"},
        headers=bearer(tenant="acme", roles=["payroll_officer"]),
    )
    assert resp.status_code == 502
    assert resp.json()["error"] == "HOST_ERROR"


def test_e2e_pf_calculate_vendor_not_supported_iceipts_409(make_client, bearer, frappe_mock):
    """(3) Negative: PF calculation on iCeipts host returns 409 VENDOR_NOT_SUPPORTED."""
    client = make_client()
    resp = client.post(
        "/v1/payroll/pf/calculate",
        json={"employee_id": "EMP-900", "period": "2026-09"},
        headers=bearer(tenant="globex", roles=["hr_manager"]),
    )
    assert resp.status_code == 409
    assert resp.json()["error"] == "VENDOR_NOT_SUPPORTED"


@pytest.mark.parametrize("bad_period", ["2026-13", "2026-00", "2026/09", "2026-9", "invalid"])
def test_e2e_pf_calculate_negative_malformed_period_422(make_client, bearer, bad_period):
    """(3) Negative: Malformed period YYYY-MM returns 422 INVALID_ARGUMENT."""
    client = make_client()
    resp = client.post(
        "/v1/payroll/pf/calculate",
        json={"employee_id": "HR-EMP-00001", "period": bad_period},
        headers=bearer(tenant="acme", roles=["payroll_officer"]),
    )
    assert resp.status_code == 422
    assert resp.json()["error"] == "INVALID_ARGUMENT"


def test_e2e_pf_calculate_negative_extra_fields_forbidden_422(make_client, bearer):
    """(3) Negative: Extra fields in calculation request are forbidden (422)."""
    client = make_client()
    resp = client.post(
        "/v1/payroll/pf/calculate",
        json={"employee_id": "HR-EMP-00001", "period": "2026-09", "extra_bonus": 5000},
        headers=bearer(tenant="acme", roles=["payroll_officer"]),
    )
    assert resp.status_code == 422


# ==============================================================================
# SECTION 6: Write-Back Two-Phase Flow
# ==============================================================================


def test_e2e_writeback_two_phase_lifecycle_propose_approve_rollback(make_client, bearer, frappe_mock):
    """(1) Positive: Full propose -> approve -> rollback flow with separation of duties."""
    client = make_client()
    propose_payload = {
        "doctype": "Employee",
        "name": "HR-EMP-00001",
        "patch": {"notice_number_of_days": 60},
    }

    # Step 1: Propose by manager 1
    resp_prop = client.post(
        "/v1/writeback/proposals",
        json=propose_payload,
        headers=bearer(tenant="acme", sub="MGR-001", roles=["hr_manager"]),
    )
    assert resp_prop.status_code == 200
    prop_data = resp_prop.json()
    proposal_id = prop_data["id"]
    assert prop_data["status"] == "PENDING"
    assert prop_data["diff"] == [{"after": 60, "before": 30, "field": "notice_number_of_days"}]

    # Step 2: Approve by manager 2 (different person, fresh token)
    resp_app = client.post(
        f"/v1/writeback/proposals/{proposal_id}/approve",
        headers=bearer(tenant="acme", sub="MGR-002", roles=["hr_manager"], iat=NOW),
    )
    assert resp_app.status_code == 200
    app_data = resp_app.json()
    assert app_data["status"] == "EXECUTED"
    assert app_data["approved_by"] == "MGR-002"
    assert frappe_mock.doc["notice_number_of_days"] == 60

    # Step 3: Rollback by manager 2 (or fresh manager)
    resp_rb = client.post(
        f"/v1/writeback/proposals/{proposal_id}/rollback",
        headers=bearer(tenant="acme", sub="MGR-002", roles=["hr_manager"], iat=NOW),
    )
    assert resp_rb.status_code == 200
    rb_data = resp_rb.json()
    assert rb_data["status"] == "ROLLED_BACK"
    assert frappe_mock.doc["notice_number_of_days"] == 30


def test_e2e_writeback_separation_of_duties_same_person_403(make_client, bearer, frappe_mock):
    """(3) Negative: Proposer attempting to approve their own proposal receives 403 SAME_PERSON."""
    client = make_client()
    propose_payload = {
        "doctype": "Employee",
        "name": "HR-EMP-00001",
        "patch": {"notice_number_of_days": 45},
    }
    resp_prop = client.post(
        "/v1/writeback/proposals",
        json=propose_payload,
        headers=bearer(tenant="acme", sub="MGR-001", roles=["hr_manager"]),
    )
    assert resp_prop.status_code == 200
    proposal_id = resp_prop.json()["id"]

    # Manager 1 attempts to approve their own proposal
    resp_app = client.post(
        f"/v1/writeback/proposals/{proposal_id}/approve",
        headers=bearer(tenant="acme", sub="MGR-001", roles=["hr_manager"], iat=NOW),
    )
    assert resp_app.status_code == 403
    assert resp_app.json()["error"] == "SAME_PERSON"


def test_e2e_writeback_freshness_boundary_300s_vs_301s(make_client, bearer, frappe_mock):
    """(2) Boundary: Token age 300s accepted; age 301s rejected with 401 REAUTH_REQUIRED."""
    client = make_client()
    resp_prop = client.post(
        "/v1/writeback/proposals",
        json={"doctype": "Employee", "name": "HR-EMP-00001", "patch": {"notice_number_of_days": 45}},
        headers=bearer(tenant="acme", sub="MGR-001", roles=["hr_manager"]),
    )
    proposal_id = resp_prop.json()["id"]

    # 301 seconds old -> 401 REAUTH_REQUIRED
    resp_stale = client.post(
        f"/v1/writeback/proposals/{proposal_id}/approve",
        headers=bearer(tenant="acme", sub="MGR-002", roles=["hr_manager"], iat=NOW - 301),
    )
    assert resp_stale.status_code == 401
    assert resp_stale.json()["error"] == "REAUTH_REQUIRED"

    # Exactly 300 seconds old -> Accepted
    resp_fresh = client.post(
        f"/v1/writeback/proposals/{proposal_id}/approve",
        headers=bearer(tenant="acme", sub="MGR-002", roles=["hr_manager"], iat=NOW - 300),
    )
    assert resp_fresh.status_code == 200
    assert resp_fresh.json()["status"] == "EXECUTED"


def test_e2e_writeback_non_writable_field_422(make_client, bearer, frappe_mock):
    """(3) Negative: Proposing change to non-allowlisted field returns 422 FIELD_NOT_WRITABLE."""
    client = make_client()
    payload = {
        "doctype": "Employee",
        "name": "HR-EMP-00001",
        "patch": {"unlisted_field": "val"},
    }
    resp = client.post(
        "/v1/writeback/proposals",
        json=payload,
        headers=bearer(tenant="acme", roles=["hr_manager"]),
    )
    assert resp.status_code == 422
    assert resp.json()["error"] == "FIELD_NOT_WRITABLE"


def test_e2e_writeback_pii_field_forbidden_422(make_client, bearer, frappe_mock):
    """(3) Negative: Proposing change to personal identifier field returns 422 FIELD_NOT_WRITABLE."""
    client = make_client()
    payload = {
        "doctype": "Employee",
        "name": "HR-EMP-00001",
        "patch": {"company_email": "new-alias"},
    }
    resp = client.post(
        "/v1/writeback/proposals",
        json=payload,
        headers=bearer(tenant="acme", roles=["hr_manager"]),
    )
    assert resp.status_code == 422
    assert resp.json()["error"] == "FIELD_NOT_WRITABLE"


@pytest.mark.parametrize("bad_val", ["not-an-int", True, 3.14, None])
def test_e2e_writeback_invalid_value_type_422(make_client, bearer, frappe_mock, bad_val):
    """(3) Negative: Proposing invalid type for integer field returns 422 INVALID_VALUE."""
    client = make_client()
    payload = {
        "doctype": "Employee",
        "name": "HR-EMP-00001",
        "patch": {"notice_number_of_days": bad_val},
    }
    resp = client.post(
        "/v1/writeback/proposals",
        json=payload,
        headers=bearer(tenant="acme", roles=["hr_manager"]),
    )
    assert resp.status_code == 422
    assert resp.json()["error"] == "INVALID_VALUE"


def test_e2e_writeback_no_change_patch_422(make_client, bearer, frappe_mock):
    """(3) Negative: Proposing patch identical to current value returns 422 NO_CHANGE."""
    client = make_client()
    # Current value in frappe_mock is 30
    payload = {
        "doctype": "Employee",
        "name": "HR-EMP-00001",
        "patch": {"notice_number_of_days": 30},
    }
    resp = client.post(
        "/v1/writeback/proposals",
        json=payload,
        headers=bearer(tenant="acme", roles=["hr_manager"]),
    )
    assert resp.status_code == 422
    assert resp.json()["error"] == "NO_CHANGE"


def test_e2e_writeback_empty_patch_422(make_client, bearer, frappe_mock):
    """(3) Negative: Empty patch dict returns 422 INVALID_VALUE."""
    client = make_client()
    payload = {
        "doctype": "Employee",
        "name": "HR-EMP-00001",
        "patch": {},
    }
    resp = client.post(
        "/v1/writeback/proposals",
        json=payload,
        headers=bearer(tenant="acme", roles=["hr_manager"]),
    )
    assert resp.status_code == 422
    assert resp.json()["error"] == "INVALID_VALUE"


def test_e2e_writeback_concurrency_conflict_on_approve_409(make_client, bearer, frappe_mock):
    """(3) Negative: Upstream document modified between propose and approve returns 409 CONFLICT."""
    client = make_client()
    resp_prop = client.post(
        "/v1/writeback/proposals",
        json={"doctype": "Employee", "name": "HR-EMP-00001", "patch": {"notice_number_of_days": 45}},
        headers=bearer(tenant="acme", sub="MGR-001", roles=["hr_manager"]),
    )
    proposal_id = resp_prop.json()["id"]

    # External modification to Frappe document
    frappe_mock.touch()

    resp_app = client.post(
        f"/v1/writeback/proposals/{proposal_id}/approve",
        headers=bearer(tenant="acme", sub="MGR-002", roles=["hr_manager"], iat=NOW),
    )
    assert resp_app.status_code == 409
    assert resp_app.json()["error"] == "CONFLICT"


def test_e2e_writeback_concurrency_conflict_on_rollback_409(make_client, bearer, frappe_mock):
    """(3) Negative: Upstream document modified after approve before rollback returns 409 ROLLBACK_CONFLICT."""
    client = make_client()
    resp_prop = client.post(
        "/v1/writeback/proposals",
        json={"doctype": "Employee", "name": "HR-EMP-00001", "patch": {"notice_number_of_days": 45}},
        headers=bearer(tenant="acme", sub="MGR-001", roles=["hr_manager"]),
    )
    proposal_id = resp_prop.json()["id"]

    resp_app = client.post(
        f"/v1/writeback/proposals/{proposal_id}/approve",
        headers=bearer(tenant="acme", sub="MGR-002", roles=["hr_manager"], iat=NOW),
    )
    assert resp_app.status_code == 200

    # External modification occurs post-approve
    frappe_mock.touch()

    resp_rb = client.post(
        f"/v1/writeback/proposals/{proposal_id}/rollback",
        headers=bearer(tenant="acme", sub="MGR-002", roles=["hr_manager"], iat=NOW),
    )
    assert resp_rb.status_code == 409
    assert resp_rb.json()["error"] == "ROLLBACK_CONFLICT"


def test_e2e_writeback_idempotency(make_client, bearer, frappe_mock):
    """(1) Positive / (2) Boundary: Re-approving or re-rolling back is idempotent without extra host calls."""
    client = make_client()
    resp_prop = client.post(
        "/v1/writeback/proposals",
        json={"doctype": "Employee", "name": "HR-EMP-00001", "patch": {"notice_number_of_days": 45}},
        headers=bearer(tenant="acme", sub="MGR-001", roles=["hr_manager"]),
    )
    proposal_id = resp_prop.json()["id"]

    # First approve
    resp_app1 = client.post(
        f"/v1/writeback/proposals/{proposal_id}/approve",
        headers=bearer(tenant="acme", sub="MGR-002", roles=["hr_manager"], iat=NOW),
    )
    assert resp_app1.status_code == 200
    puts_count_after_approve = len(frappe_mock.puts)

    # Second approve -> idempotent
    resp_app2 = client.post(
        f"/v1/writeback/proposals/{proposal_id}/approve",
        headers=bearer(tenant="acme", sub="MGR-002", roles=["hr_manager"], iat=NOW),
    )
    assert resp_app2.status_code == 200
    assert resp_app2.json() == resp_app1.json()
    assert len(frappe_mock.puts) == puts_count_after_approve  # no second PUT

    # First rollback
    resp_rb1 = client.post(
        f"/v1/writeback/proposals/{proposal_id}/rollback",
        headers=bearer(tenant="acme", sub="MGR-002", roles=["hr_manager"], iat=NOW),
    )
    assert resp_rb1.status_code == 200
    puts_count_after_rollback = len(frappe_mock.puts)

    # Second rollback -> idempotent
    resp_rb2 = client.post(
        f"/v1/writeback/proposals/{proposal_id}/rollback",
        headers=bearer(tenant="acme", sub="MGR-002", roles=["hr_manager"], iat=NOW),
    )
    assert resp_rb2.status_code == 200
    assert resp_rb2.json() == resp_rb1.json()
    assert len(frappe_mock.puts) == puts_count_after_rollback  # no third PUT


def test_e2e_writeback_tenant_isolation_404(make_client, bearer, frappe_mock):
    """(3) Negative: Tenant globex cannot view or approve proposals belonging to tenant acme (404)."""
    client = make_client()
    resp_prop = client.post(
        "/v1/writeback/proposals",
        json={"doctype": "Employee", "name": "HR-EMP-00001", "patch": {"notice_number_of_days": 45}},
        headers=bearer(tenant="acme", sub="MGR-001", roles=["hr_manager"]),
    )
    proposal_id = resp_prop.json()["id"]

    # Globex tries to approve
    resp_cross = client.post(
        f"/v1/writeback/proposals/{proposal_id}/approve",
        headers=bearer(tenant="globex", sub="MGR-002", roles=["hr_manager"], iat=NOW),
    )
    assert resp_cross.status_code == 404
    assert resp_cross.json()["error"] == "NOT_FOUND"


def test_e2e_writeback_vendor_not_supported_iceipts_409(make_client, bearer, frappe_mock):
    """(3) Negative: Writeback proposal for iceipts host returns 409 VENDOR_NOT_SUPPORTED."""
    client = make_client()
    payload = {
        "doctype": "Employee",
        "name": "HR-EMP-00001",
        "patch": {"notice_number_of_days": 45},
    }
    resp = client.post(
        "/v1/writeback/proposals",
        json=payload,
        headers=bearer(tenant="globex", roles=["hr_manager"]),
    )
    assert resp.status_code == 409
    assert resp.json()["error"] == "VENDOR_NOT_SUPPORTED"


# ==============================================================================
# SECTION 7: /v1/remediate/execute Endpoint
# ==============================================================================


def test_e2e_remediate_execute_positive_attribution_only(make_client, bearer):
    """(1) Positive: /v1/remediate/execute issues signed host audit headers and writes nothing."""
    client = make_client()
    payload = {
        "violation_id": "VIO-LEAVE-001",
        "target_entity": "EMPLOYEE",
        "entity_id": "EMP-900",
        "patch_payload": {"annual_leave_balance": 24},
    }
    resp = client.post(
        "/v1/remediate/execute",
        json=payload,
        headers=bearer(tenant="acme", sub="AUDITOR-42", roles=["hr_manager"], iat=NOW),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ATTRIBUTION_ONLY"
    assert data["applied"] is False
    assert data["violation_id"] == "VIO-LEAVE-001"
    assert data["entity_id"] == "EMP-900"

    trace = data["audit_tracing"]
    assert trace["X-HITL-Approver-Id"] == "AUDITOR-42"
    assert trace["X-Agent-Name"] == "StatutoryComplianceAuditor"
    assert trace["X-Tenant-Id"] == "acme"
    assert "X-Agent-Invocation-Id" in trace
    assert "X-Reasoning-Hash" in trace


def test_e2e_remediate_execute_freshness_boundary_300s_vs_301s(make_client, bearer):
    """(2) Boundary: Token age 300s accepted; age 301s returns 401 REAUTH_REQUIRED."""
    client = make_client()
    payload = {
        "violation_id": "VIO-001",
        "target_entity": "EMPLOYEE",
        "entity_id": "EMP-1",
        "patch_payload": {"p": 1},
    }
    # 300s old -> 200
    resp_fresh = client.post(
        "/v1/remediate/execute",
        json=payload,
        headers=bearer(tenant="acme", roles=["hr_manager"], iat=NOW - 300),
    )
    assert resp_fresh.status_code == 200

    # 301s old -> 401
    resp_stale = client.post(
        "/v1/remediate/execute",
        json=payload,
        headers=bearer(tenant="acme", roles=["hr_manager"], iat=NOW - 301),
    )
    assert resp_stale.status_code == 401
    assert resp_stale.json()["error"] == "REAUTH_REQUIRED"


@pytest.mark.parametrize(
    "extra_field",
    [{"approver_id": "spoofed-user"}, {"approver_role": "SUPER_ADMIN"}, {"client_timestamp": 12345}],
)
def test_e2e_remediate_execute_negative_extra_fields_forbidden_422(make_client, bearer, extra_field):
    """(3) Negative: Extra fields in remediation body fail closed with 422 (extra='forbid')."""
    client = make_client()
    payload = {
        "violation_id": "VIO-001",
        "target_entity": "EMPLOYEE",
        "entity_id": "EMP-1",
        "patch_payload": {"p": 1},
        **extra_field,
    }
    resp = client.post(
        "/v1/remediate/execute",
        json=payload,
        headers=bearer(tenant="acme", roles=["hr_manager"], iat=NOW),
    )
    assert resp.status_code == 422


# ==============================================================================
# SECTION 8: /v1/hosts/requisitions Endpoint
# ==============================================================================


def test_e2e_hosts_requisitions_frappe_profile(make_client, bearer, frappe_mock):
    """(1) Positive / (2) Boundary: GET /v1/hosts/requisitions parses Frappe Job Requisitions."""
    # Add an unmapped status to verify rejected reporting
    frappe_mock.job_requisitions.append(
        {
            "name": "HR-HIREQ-00003",
            "designation": "Intern",
            "department": "Engineering",
            "no_of_positions": 2,
            "status": "CustomStatusNotMapped",
            "description": "Internship role",
            "requested_by": "HR-EMP-00004",
        }
    )
    client = make_client()
    resp = client.get("/v1/hosts/requisitions", headers=bearer(tenant="acme", roles=["recruiter"]))
    assert resp.status_code == 200
    data = resp.json()
    assert data["vendor"] == "frappe"
    reqs = data["requisitions"]
    assert len(reqs) == 2
    assert reqs[0]["id"] == "HR-HIREQ-00001"
    assert reqs[0]["title"] == "Software Engineer"
    assert reqs[0]["headcount"] == 3
    assert reqs[0]["status"] == "OPEN"  # mapped from 'Open & Approved'
    assert reqs[1]["status"] == "PENDING"  # mapped from 'Pending'

    # Unmapped status record in rejected array
    rejected = data["rejected"]
    assert len(rejected) == 1
    assert rejected[0]["id"] == "HR-HIREQ-00003"
    assert rejected[0]["error"] == "UNMAPPED_VALUE"
    assert rejected[0]["field"] == "status"


def test_e2e_hosts_requisitions_iceipts_profile(make_client, bearer, host_environments, monkeypatch):
    """(1) Positive / (2) Boundary: GET /v1/hosts/requisitions parses iCeipts paginated requisitions."""
    iceipts_pages = [
        {
            "success": True,
            "status": 200,
            "data": {
                "pageData": [
                    {
                        "id": "req-ice-1",
                        "title": "Cloud Architect",
                        "status": "JOB_CONVERTED",
                        "location": "Dubai",
                        "position": "2",
                        "jobDescription": "Manage Cloud",
                        "department": {"name": "Infrastructure"},
                        "managerId": "MGR-ICE-88",
                    },
                    {
                        "id": "req-ice-2",
                        "title": "QA Engineer",
                        "status": "APPROVED",
                        "location": "Abu Dhabi",
                        "position": "invalid-headcount",
                        "jobDescription": "Test Everything",
                        "department": {"name": "QA"},
                        "managerId": "MGR-ICE-99",
                    },
                ],
                "totalPages": 1,
                "currentPage": 1,
                "totalrows": 2,
            },
        }
    ]
    iceipts_mock = MockIceiptsServer(pages=iceipts_pages)
    monkeypatch.setattr(declarative, "_default_transport", lambda: httpx.MockTransport(iceipts_mock.handler))

    client = make_client()
    resp = client.get("/v1/hosts/requisitions", headers=bearer(tenant="globex", roles=["hr_manager"]))
    assert resp.status_code == 200
    data = resp.json()
    assert data["vendor"] == "iceipts"
    assert len(data["requisitions"]) == 1
    req = data["requisitions"][0]
    assert req["id"] == "req-ice-1"
    assert req["title"] == "Cloud Architect"
    assert req["headcount"] == 2
    assert req["status"] == "OPEN"  # JOB_CONVERTED -> OPEN
    assert req["department"] == "Infrastructure"
    assert req["hiring_manager_ref"] == "MGR-ICE-88"

    # Invalid headcount position -> rejected
    assert len(data["rejected"]) == 1
    assert data["rejected"][0]["id"] == "req-ice-2"
    assert data["rejected"][0]["error"] == "INVALID_VALUE"
    assert data["rejected"][0]["field"] == "headcount"


def test_e2e_hosts_requisitions_host_error_upstream_500_502(make_client, bearer, frappe_mock):
    """(3) Negative: Upstream host failure on requisitions returns 502 HOST_ERROR."""
    frappe_mock.status = 500
    client = make_client()
    resp = client.get("/v1/hosts/requisitions", headers=bearer(tenant="acme", roles=["recruiter"]))
    assert resp.status_code == 502
    assert resp.json()["error"] == "HOST_ERROR"


def test_e2e_hosts_requisitions_host_not_configured_409(make_client, bearer, monkeypatch):
    """(3) Negative: Tenant with no host configured returns 409 HOST_NOT_CONFIGURED."""
    monkeypatch.delenv("HRMS_HOSTS", raising=False)
    client = make_client()
    resp = client.get("/v1/hosts/requisitions", headers=bearer(tenant="acme", roles=["recruiter"]))
    assert resp.status_code == 409
    assert resp.json()["error"] == "HOST_NOT_CONFIGURED"
