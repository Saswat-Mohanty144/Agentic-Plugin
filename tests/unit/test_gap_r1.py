"""Tests for Review round findings R1, R2, R3, R6, R7, R9 (Task r1)."""

import hashlib
import json
import time
from typing import Any, Dict

import httpx
import pytest
import yaml
from pydantic import ValidationError

from hrms_plugin.agents.supervisor import SupervisorAgent
from hrms_plugin.integrations.frappe import FrappeClient
from hrms_plugin.payroll.service import PayrollService
from hrms_plugin.rag.store import Jurisdiction
from hrms_plugin.security.rbac import AuthContext, UserRole
from hrms_plugin.security.tenant_registry import LlmPolicy
from hrms_plugin.server import app as app_module
from hrms_plugin.server.app import IntrospectRequest, SynthesizeRequest, make_classification_resolver
from hrms_plugin.storage.mapping_store import MappingStore
from hrms_plugin.storage.memory_store import MemoryKeyValueStore
from hrms_plugin.writeback.service import WritebackError, WritebackService

NOW = 1_700_000_000.0

ADMIN_USER = AuthContext(user_id="admin-1", tenant_id="acme", roles=frozenset({UserRole.SUPER_ADMIN}))


def _rpc_req(method: str, params: Any = None, req_id: Any = "1") -> Dict[str, Any]:
    req: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        req["params"] = params
    if req_id is not None:
        req["id"] = req_id
    return req


def _headers(bearer_headers: Dict[str, str], jurisdiction: str = "IN", origin: str = None) -> Dict[str, str]:
    h = dict(bearer_headers)
    if jurisdiction is not None:
        h["X-HRMS-Jurisdiction"] = jurisdiction
    if origin is not None:
        h["Origin"] = origin
    return h


# =============================================================================
# R1: SynthesizeRequest & IntrospectRequest vendor fallback & resolver
# =============================================================================


def test_r1_synthesize_and_introspect_vendor_optional_and_pattern():
    """vendor field is Optional[str] defaulting to None; non-empty values are validated."""
    # SynthesizeRequest: default is None
    req_syn = SynthesizeRequest(domain="EMPLOYEE", discovered_fields=[{"name": "full_name"}])
    assert req_syn.vendor is None

    # SynthesizeRequest: explicit valid value kept
    req_syn_exp = SynthesizeRequest(
        domain="EMPLOYEE", discovered_fields=[{"name": "full_name"}], vendor="frappe"
    )
    assert req_syn_exp.vendor == "frappe"

    # SynthesizeRequest: invalid pattern rejected
    with pytest.raises(ValidationError):
        SynthesizeRequest(domain="EMPLOYEE", discovered_fields=[], vendor="Bad Vendor!")

    # IntrospectRequest: default is None
    req_intro = IntrospectRequest(swagger_dict={"swagger": "2.0"})
    assert req_intro.vendor is None

    # IntrospectRequest: explicit valid value kept
    req_intro_exp = IntrospectRequest(swagger_dict={"swagger": "2.0"}, vendor="iceipts")
    assert req_intro_exp.vendor == "iceipts"

    # IntrospectRequest: invalid pattern rejected
    with pytest.raises(ValidationError):
        IntrospectRequest(swagger_dict={}, vendor="Bad:Vendor")


def test_r1_synthesize_without_vendor_uses_host_vendor_and_resolves_in_chat(
    make_client, bearer, tmp_path, monkeypatch
):
    """Synthesize without vendor adopts tenant host vendor; classification resolver finds it."""
    hosts_cfg = {
        "tenants": [
            {
                "tenant_id": "acme",
                "vendor": "frappe",
                "base_url": "https://hr.acme.test",
                "api_key_env": "ACME_KEY",
                "api_secret_env": "ACME_SECRET",
                "timeout_seconds": 10,
            }
        ]
    }
    hosts_file = tmp_path / "hosts.yaml"
    hosts_file.write_text(yaml.safe_dump(hosts_cfg))
    monkeypatch.setenv("HRMS_HOSTS", str(hosts_file))

    store = MappingStore(backend=MemoryKeyValueStore())
    monkeypatch.setattr(app_module, "mapping_store", store)
    client = make_client()
    alice = bearer(roles=["platform_admin"], sub="alice")
    bob = bearer(roles=["platform_admin"], sub="bob")

    # Synthesize request without vendor
    payload = {
        "domain": "EMPLOYEE",
        "discovered_fields": [{"name": "department"}, {"name": "designation"}],
    }
    res = client.post("/v1/schema/synthesize", json=payload, headers=alice)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["vendor"] == "frappe"
    version = body["mapping_version"]

    # Approve with field classification
    classification = {"department": {"data_class": "INTERNAL", "llm_visible": True}}
    res_app = client.post(
        f"/v1/schema/mappings/frappe/EMPLOYEE/versions/{version}/approve",
        json={"classification": classification},
        headers=bob,
    )
    assert res_app.status_code == 200, res_app.text

    # make_classification_resolver returns the classification
    resolver = make_classification_resolver(store)
    active_cls = resolver("acme", "EMPLOYEE")
    assert active_cls is not None
    assert active_cls.get("department") == {"data_class": "INTERNAL", "llm_visible": True}


def test_r1_synthesize_vendor_fallback_without_configured_host(make_client, bearer, monkeypatch):
    """When HRMS_HOSTS has no config for the tenant, synthesize without vendor falls back to 'host'."""
    monkeypatch.delenv("HRMS_HOSTS", raising=False)
    store = MappingStore(backend=MemoryKeyValueStore())
    monkeypatch.setattr(app_module, "mapping_store", store)
    client = make_client()
    alice = bearer(roles=["platform_admin"], sub="alice")

    payload = {
        "domain": "EMPLOYEE",
        "discovered_fields": [{"name": "employee_name"}],
    }
    res = client.post("/v1/schema/synthesize", json=payload, headers=alice)
    assert res.status_code == 200
    assert res.json()["vendor"] == "host"

    # Explicit vendor overrides fallback
    res_exp = client.post("/v1/schema/synthesize", json={**payload, "vendor": "custom_crm"}, headers=alice)
    assert res_exp.status_code == 200
    assert res_exp.json()["vendor"] == "custom_crm"


# =============================================================================
# R2: LlmPolicy mcp_enabled, gating on /v1/mcp, and tools/call EgressLog
# =============================================================================


def test_r2_llm_policy_mcp_enabled_default_false():
    """LlmPolicy gains mcp_enabled: bool = False."""
    pol = LlmPolicy(allowed_providers=["fake"])
    assert pol.mcp_enabled is False

    pol_true = LlmPolicy(allowed_providers=["fake"], mcp_enabled=True)
    assert pol_true.mcp_enabled is True


def test_r2_mcp_initialize_and_ping_proceed_without_mcp_enabled(make_client, bearer):
    """initialize and ping proceed even when tenant does not have mcp_enabled."""
    client = make_client()
    globex_headers = _headers(bearer(tenant="globex", roles=["staff"]), jurisdiction="IN")

    # ping proceeds
    res_ping = client.post("/v1/mcp", json=_rpc_req("ping"), headers=globex_headers)
    assert res_ping.status_code == 200
    assert res_ping.json()["result"] == {}

    # initialize proceeds
    res_init = client.post(
        "/v1/mcp",
        json=_rpc_req("initialize", {"protocolVersion": "2025-06-18"}),
        headers=globex_headers,
    )
    assert res_init.status_code == 200
    assert res_init.json()["result"]["serverInfo"]["name"] == "hrms-agentic-plugin"


def test_r2_mcp_tools_list_and_call_gated_by_policy(make_client, bearer, keys):
    """tools/list and tools/call require tenant policy, mcp_enabled=True, max_data_tier>=2."""
    client = make_client()

    # 1. globex tenant has mcp_enabled=False -> 403 MCP_NOT_ALLOWED_FOR_TENANT
    globex_headers = _headers(bearer(tenant="globex", roles=["staff"]), jurisdiction="IN")
    res_list = client.post("/v1/mcp", json=_rpc_req("tools/list"), headers=globex_headers)
    assert res_list.status_code == 403
    assert res_list.json() == {"error": "MCP_NOT_ALLOWED_FOR_TENANT"}

    res_call = client.post(
        "/v1/mcp",
        json=_rpc_req("tools/call", {"name": "search_legal_kb", "arguments": {"query": "leave"}}),
        headers=globex_headers,
    )
    assert res_call.status_code == 403
    assert res_call.json() == {"error": "MCP_NOT_ALLOWED_FOR_TENANT"}

    # 2. initech tenant has no llm_policy at all -> 403 MCP_NOT_ALLOWED_FOR_TENANT
    from tests.support.auth import INITECH_ISSUER, claims, mint

    tok_initech = mint(
        keys.initech_rsa,
        "initech-rsa",
        claims(
            issuer=INITECH_ISSUER,
            tenant="initech",
            roles=["hr"],
            tenant_claim="org",
            roles_claim="groups",
            lifetime=60,
        ),
    )
    initech_headers = _headers({"Authorization": f"Bearer {tok_initech}"}, jurisdiction="IN")
    res_ini_list = client.post("/v1/mcp", json=_rpc_req("tools/list"), headers=initech_headers)
    assert res_ini_list.status_code == 403
    assert res_ini_list.json() == {"error": "MCP_NOT_ALLOWED_FOR_TENANT"}

    # 3. acme tenant has mcp_enabled=True and max_data_tier=2 -> tools/list and tools/call succeed
    acme_headers = _headers(bearer(tenant="acme", roles=["staff"]), jurisdiction="IN")
    res_acme_list = client.post("/v1/mcp", json=_rpc_req("tools/list"), headers=acme_headers)
    assert res_acme_list.status_code == 200
    assert "tools" in res_acme_list.json()["result"]


def test_r2_mcp_tools_call_writes_egress_log(make_client, bearer, monkeypatch):
    """Every tools/call writes an egress row: provider 'mcp-client', model=tool name, payload hash, status."""
    client = make_client()
    egress_store = MemoryKeyValueStore()
    from hrms_plugin.governance.egress_log import EgressLog
    log = EgressLog(egress_store)
    monkeypatch.setattr(app_module, "egress_log", log)
    if hasattr(client.app.state, "egress_log"):
        client.app.state.egress_log = log

    acme_headers = _headers(bearer(tenant="acme", roles=["staff"]), jurisdiction="IN")
    args = {"query": "maternity"}
    res_call = client.post(
        "/v1/mcp",
        json=_rpc_req("tools/call", {"name": "hrms_search_labor_regulations", "arguments": args}),
        headers=acme_headers,
    )
    assert res_call.status_code == 200

    # Verify egress log entry
    rows = log.list("acme")
    assert len(rows) >= 1
    mcp_row = next(
        (r for r in rows if r["provider"] == "mcp-client" and r["model"] == "hrms_search_labor_regulations"),
        None,
    )
    assert mcp_row is not None
    assert mcp_row["status"] == "COMPLETED"
    expected_hash = "sha256:" + hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()
    assert mcp_row["payload_sha256"] == expected_hash

    # Failed tool execution records status FAILED
    bad_args = {"unknown_arg": "invalid"}
    res_bad = client.post(
        "/v1/mcp",
        json=_rpc_req("tools/call", {"name": "hrms_nonexistent_tool", "arguments": bad_args}),
        headers=acme_headers,
    )
    assert res_bad.status_code == 200
    assert res_bad.json()["result"]["isError"] is True
    rows_after = log.list("acme")
    failed_row = next((r for r in rows_after if r["status"] == "FAILED"), None)
    assert failed_row is not None


# =============================================================================
# R3: 24 h expiry for pending two-person requests: rollback, fingerprint, shape
# =============================================================================


class FakeClock:
    def __init__(self, t: float = 1_000_000.0):
        self.now = t

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float):
        self.now += seconds


class StatefulFrappeDoc:
    def __init__(self):
        self.doc = {
            "name": "HR-EMP-00001",
            "employee_name": "Sunita Verma",
            "notice_number_of_days": 30,
            "modified": "2026-10-01 10:00:00.000001",
        }
        self.version = 1

    def touch(self):
        self.version += 1
        self.doc["modified"] = f"2026-10-01 10:00:00.{self.version:06d}"

    async def handler(self, request):
        if request.url.path != "/api/resource/Employee/HR-EMP-00001":
            return httpx.Response(404, json={})
        if request.method == "GET":
            return httpx.Response(200, json={"data": dict(self.doc)})
        if request.method == "PUT":
            body = json.loads(request.content)
            self.doc.update(body)
            self.touch()
            return httpx.Response(200, json={"data": dict(self.doc)})
        return httpx.Response(405, json={})


@pytest.mark.asyncio
async def test_r3_rollback_24h_expiry_boundaries(tmp_path, monkeypatch):
    """Writeback rollback 24 h expiry: valid at 24 h; expired at >24 h counts as absent (202 new)."""
    hosts_cfg = {
        "tenants": [
            {
                "tenant_id": "acme",
                "vendor": "frappe",
                "base_url": "https://hr.acme.test",
                "api_key_env": "ACME_KEY",
                "api_secret_env": "ACME_SECRET",
                "timeout_seconds": 10,
                "writable_fields": {"Employee": {"notice_number_of_days": "int"}},
            }
        ]
    }
    hosts_file = tmp_path / "hosts.yaml"
    hosts_file.write_text(yaml.safe_dump(hosts_cfg))
    monkeypatch.setenv("HRMS_HOSTS", str(hosts_file))
    monkeypatch.setenv("ACME_KEY", "k")
    monkeypatch.setenv("ACME_SECRET", "s")

    clock = FakeClock(1_000_000.0)
    fake_doc = StatefulFrappeDoc()
    transport = httpx.MockTransport(fake_doc.handler)
    http_cli = httpx.AsyncClient(transport=transport)

    def client_factory(host, key, secret):
        return FrappeClient(host, key, secret, http_client=http_cli)

    store = MemoryKeyValueStore()
    svc = WritebackService(store=store, client_factory=client_factory, clock=clock)

    proposer = AuthContext(user_id="alice", tenant_id="acme", roles=frozenset({UserRole.HR_ADMIN}))
    approver = AuthContext(user_id="bob", tenant_id="acme", roles=frozenset({UserRole.HR_ADMIN}))
    confirmer = AuthContext(user_id="charlie", tenant_id="acme", roles=frozenset({UserRole.HR_ADMIN}))

    # Propose and approve
    p = await svc.propose(proposer, "Employee", "HR-EMP-00001", {"notice_number_of_days": 60})
    await svc.approve(approver, p["id"])

    # 1. Alice requests rollback at t0 -> 202 ROLLBACK_PENDING
    res1 = await svc.rollback(proposer, p["id"])
    assert res1["status"] == "ROLLBACK_PENDING"

    # Alice again before 24h -> 403 SAME_PERSON
    clock.advance(100.0)
    with pytest.raises(WritebackError) as exc:
        await svc.rollback(proposer, p["id"])
    assert exc.value.code == "SAME_PERSON"

    # Just under / exactly 24h: 86400 - 100 seconds advanced
    clock.advance(86400.0 - 100.0)  # total elapsed exactly 86400
    # At exactly 24h, Bob (different person) confirms -> executes rollback
    res_conf = await svc.rollback(confirmer, p["id"])
    assert res_conf["status"] == "ROLLED_BACK"

    # 2. Test expiry on a new proposal:
    p2 = await svc.propose(proposer, "Employee", "HR-EMP-00001", {"notice_number_of_days": 45})
    await svc.approve(approver, p2["id"])

    # Alice requests rollback at current clock
    await svc.rollback(proposer, p2["id"])
    # Advance past 24h (+ 86401 seconds)
    clock.advance(86401.0)

    # Expired pending request counts as absent: Alice calling again records a new pending request (202)
    res_exp = await svc.rollback(proposer, p2["id"])
    assert res_exp["status"] == "ROLLBACK_PENDING"
    assert res_exp["rollback_requested_by"] == "alice"


def test_r3_fingerprint_24h_expiry_boundaries():
    """Payroll fingerprint approve 24 h expiry: valid at 24 h; expired at >24 h counts as absent."""
    store = MemoryKeyValueStore()
    clock = FakeClock(1_000_000.0)
    svc = PayrollService(store=store, clock=clock)

    fp = "sha256:" + "a" * 64
    # 1. User A requests at t0
    r1 = svc.approve_fingerprint("acme", "frappe", "Salary Slip", fp, "user-A")
    assert r1["status"] == "PENDING_CONFIRMATION"

    # Same user at t0 + 10s -> SAME_PERSON
    clock.advance(10.0)
    assert svc.approve_fingerprint("acme", "frappe", "Salary Slip", fp, "user-A") == {"error": "SAME_PERSON"}

    # Exactly 24h (total 86400s) -> User B can approve
    clock.advance(86400.0 - 10.0)
    r_app = svc.approve_fingerprint("acme", "frappe", "Salary Slip", fp, "user-B")
    assert r_app["status"] == "APPROVED"

    # 2. Expiry test on a second fingerprint
    fp2 = "sha256:" + "b" * 64
    svc.approve_fingerprint("acme", "frappe", "Salary Slip", fp2, "user-A")
    # Advance past 24h
    clock.advance(86401.0)

    # User A requests again: expired counts as absent, records new request
    r_re = svc.approve_fingerprint("acme", "frappe", "Salary Slip", fp2, "user-A")
    assert r_re["status"] == "PENDING_CONFIRMATION"
    assert r_re["requested_by"] == "user-A"

    # User C requests after another 24h + 1s -> records new request by User C
    clock.advance(86401.0)
    r_c = svc.approve_fingerprint("acme", "frappe", "Salary Slip", fp2, "user-C")
    assert r_c["status"] == "PENDING_CONFIRMATION"
    assert r_c["requested_by"] == "user-C"


def test_r3_shape_24h_expiry_boundaries(make_client, bearer, tmp_path, monkeypatch):
    """Requisition shape approve 24 h expiry: valid at 24 h; expired at >24 h counts as absent."""
    hosts_cfg = {
        "tenants": [
            {
                "tenant_id": "acme",
                "vendor": "frappe",
                "base_url": "https://hr.acme.test",
                "api_key_env": "ACME_KEY",
                "api_secret_env": "ACME_SECRET",
                "timeout_seconds": 10,
            }
        ]
    }
    hosts_file = tmp_path / "hosts.yaml"
    hosts_file.write_text(yaml.safe_dump(hosts_cfg))
    monkeypatch.setenv("HRMS_HOSTS", str(hosts_file))

    store = MemoryKeyValueStore()
    monkeypatch.setattr(app_module, "_storage", store)
    clock = FakeClock(1_000_000.0)
    monkeypatch.setattr(time, "time", clock)

    client = make_client()
    if hasattr(client.app.state, "clock"):
        client.app.state.clock = clock

    alice = bearer(roles=["platform_admin"], sub="alice")
    bob = bearer(roles=["platform_admin"], sub="bob")

    fp = "sha256:" + "c" * 64
    store.set("observed_shapes", "acme:frappe:REQUISITION", {"fingerprint": fp, "observed_at": clock()})

    # Alice requests at t0 -> 202 PENDING_CONFIRMATION
    res1 = client.post("/v1/hosts/requisitions/shape/approve", json={"fingerprint": fp}, headers=alice)
    assert res1.status_code == 202
    assert res1.json()["status"] == "PENDING_CONFIRMATION"

    # Alice again before 24h -> 403 SAME_PERSON
    clock.advance(10.0)
    res_same = client.post("/v1/hosts/requisitions/shape/approve", json={"fingerprint": fp}, headers=alice)
    assert res_same.status_code == 403
    assert res_same.json() == {"error": "SAME_PERSON"}

    # Exactly 24h -> Bob approves -> 200 APPROVED
    clock.advance(86400.0 - 10.0)
    res_ok = client.post("/v1/hosts/requisitions/shape/approve", json={"fingerprint": fp}, headers=bob)
    assert res_ok.status_code == 200
    assert res_ok.json()["status"] == "APPROVED"

    # Expiry test: observe new fingerprint
    fp2 = "sha256:" + "d" * 64
    store.set("observed_shapes", "acme:frappe:REQUISITION", {"fingerprint": fp2, "observed_at": clock()})
    client.post("/v1/hosts/requisitions/shape/approve", json={"fingerprint": fp2}, headers=alice)

    # Advance past 24h
    clock.advance(86401.0)
    # Alice calling again: expired counts as absent -> records new pending request (202)
    res_re = client.post("/v1/hosts/requisitions/shape/approve", json={"fingerprint": fp2}, headers=alice)
    assert res_re.status_code == 202
    assert res_re.json()["status"] == "PENDING_CONFIRMATION"
    assert res_re.json()["requested_by"] == "alice"


# =============================================================================
# R6: MCP Origin check against caller tenant's allowed_origins
# =============================================================================


def test_r6_mcp_origin_check_uses_caller_tenant_allowed_origins(make_client, bearer):
    """Origin must be in caller tenant's allowed_origins, not union across tenants."""
    client = make_client()
    # acme has allowed_origin https://hr.acme.test
    # globex has allowed_origin https://hr.globex.test
    acme_token = bearer(tenant="acme", roles=["staff"])

    # 1. acme token with globex origin -> 403 ORIGIN_NOT_ALLOWED
    res_cross = client.post(
        "/v1/mcp",
        json=_rpc_req("ping"),
        headers=_headers(acme_token, jurisdiction="IN", origin="https://hr.globex.test"),
    )
    assert res_cross.status_code == 403
    assert res_cross.json() == {"error": "ORIGIN_NOT_ALLOWED"}

    # 2. acme token with acme origin -> 200 OK
    res_acme = client.post(
        "/v1/mcp",
        json=_rpc_req("ping"),
        headers=_headers(acme_token, jurisdiction="IN", origin="https://hr.acme.test"),
    )
    assert res_acme.status_code == 200
    assert res_acme.json()["result"] == {}

    # 3. globex token with acme origin -> 403 ORIGIN_NOT_ALLOWED
    globex_token = bearer(tenant="globex", roles=["staff"])
    res_globex_cross = client.post(
        "/v1/mcp",
        json=_rpc_req("ping"),
        headers=_headers(globex_token, jurisdiction="IN", origin="https://hr.acme.test"),
    )
    assert res_globex_cross.status_code == 403
    assert res_globex_cross.json() == {"error": "ORIGIN_NOT_ALLOWED"}

    # 4. Unknown origin -> 403 ORIGIN_NOT_ALLOWED
    res_unknown = client.post(
        "/v1/mcp",
        json=_rpc_req("ping"),
        headers=_headers(acme_token, jurisdiction="IN", origin="https://attacker.example"),
    )
    assert res_unknown.status_code == 403
    assert res_unknown.json() == {"error": "ORIGIN_NOT_ALLOWED"}


# =============================================================================
# R7: Introspect with ':' in entity name returns 400 INVALID_KEY (not 500)
# =============================================================================


def test_r7_introspect_with_colon_in_entity_name_returns_400_invalid_key(make_client, bearer):
    """POST /v1/schema/introspect with ':' in entity name returns 400 INVALID_KEY."""
    client = make_client()
    auth = bearer(roles=["platform_admin"])
    payload = {
        "sample_records": {
            "Employee:Draft": [{"name": "Jane", "dept": "HR"}],
        }
    }
    res = client.post("/v1/schema/introspect", json=payload, headers=auth)
    assert res.status_code == 400
    assert res.json() == {"error": "INVALID_KEY"}


# =============================================================================
# R9: Supervisor currency derivation only for IN/AE; missing for SA/US
# =============================================================================


def test_r9_supervisor_onboarding_currency_derivation_and_missing_input():
    """Currency is derived only for IN (INR) and AE (AED); SA and US require currency."""
    sup = SupervisorAgent()

    # 1. India: currency omitted -> derived as INR, succeeds
    res_in = sup.process_message(
        message="Evaluate offer letter for candidate",
        jurisdiction=Jurisdiction.INDIA,
        auth=ADMIN_USER,
        context={"candidate_name": "Deepak", "job_title": "Developer", "annual_salary": 1200000.0},
    )
    assert res_in.confidence_score > 0.0
    assert res_in.structured_data.get("currency") == "INR"

    # 2. UAE: currency omitted -> derived as AED, succeeds
    res_ae = sup.process_message(
        message="Evaluate offer letter for candidate",
        jurisdiction=Jurisdiction.UAE,
        auth=ADMIN_USER,
        context={"candidate_name": "Tariq", "job_title": "Developer", "annual_salary": 240000.0},
    )
    assert res_ae.confidence_score > 0.0
    assert res_ae.structured_data.get("currency") == "AED"

    # 3. Saudi Arabia without currency -> MISSING_INPUT with currency
    res_sa_missing = sup.process_message(
        message="Evaluate offer letter for candidate",
        jurisdiction=Jurisdiction.SAUDI_ARABIA,
        auth=ADMIN_USER,
        context={"candidate_name": "Omar", "job_title": "Developer", "annual_salary": 180000.0},
    )
    assert res_sa_missing.confidence_score == 0.0
    assert res_sa_missing.structured_data["error"] == "MISSING_INPUT"
    assert "currency" in res_sa_missing.structured_data["missing"]

    # Saudi Arabia with currency -> succeeds with SAR
    res_sa_ok = sup.process_message(
        message="Evaluate offer letter for candidate",
        jurisdiction=Jurisdiction.SAUDI_ARABIA,
        auth=ADMIN_USER,
        context={
            "candidate_name": "Omar",
            "job_title": "Developer",
            "annual_salary": 180000.0,
            "currency": "SAR",
        },
    )
    assert res_sa_ok.confidence_score > 0.0
    assert res_sa_ok.structured_data.get("currency") == "SAR"

    # 4. US without currency -> MISSING_INPUT with currency
    res_us_missing = sup.process_message(
        message="Evaluate offer letter for candidate",
        jurisdiction=Jurisdiction.USA,
        auth=ADMIN_USER,
        context={"candidate_name": "Alice", "job_title": "Engineer", "annual_salary": 120000.0},
    )
    assert res_us_missing.confidence_score == 0.0
    assert res_us_missing.structured_data["error"] == "MISSING_INPUT"
    assert "currency" in res_us_missing.structured_data["missing"]

    # US with currency -> succeeds with USD
    res_us_ok = sup.process_message(
        message="Evaluate offer letter for candidate",
        jurisdiction=Jurisdiction.USA,
        auth=ADMIN_USER,
        context={
            "candidate_name": "Alice",
            "job_title": "Engineer",
            "annual_salary": 120000.0,
            "currency": "USD",
        },
    )
    assert res_us_ok.confidence_score > 0.0
    assert res_us_ok.structured_data.get("currency") == "USD"


def test_r9_supervisor_travel_expense_currency_derivation_and_missing_input():
    """Travel expense currency derived for IN/AE; SA and US require currency."""
    sup = SupervisorAgent()
    base_ctx = {
        "claim_id": "CLM-100",
        "employee_id": "EMP-01",
        "expenses": [{"id": "E1", "category": "MEAL", "amount": 100.0, "has_receipt": True}],
        "tier": "STANDARD",
        "has_approved_travel_request": True,
    }

    # 1. India: currency omitted -> derived as INR, succeeds
    res_in = sup.process_message(
        message="Audit travel expense claim",
        jurisdiction=Jurisdiction.INDIA,
        auth=ADMIN_USER,
        context=base_ctx,
    )
    assert res_in.confidence_score > 0.0
    assert res_in.structured_data.get("currency") == "INR"

    # 2. UAE: currency omitted -> derived as AED, succeeds
    res_ae = sup.process_message(
        message="Audit travel expense claim",
        jurisdiction=Jurisdiction.UAE,
        auth=ADMIN_USER,
        context=base_ctx,
    )
    assert res_ae.confidence_score > 0.0
    assert res_ae.structured_data.get("currency") == "AED"

    # 3. Saudi Arabia without currency -> MISSING_INPUT with currency
    res_sa_missing = sup.process_message(
        message="Audit travel expense claim",
        jurisdiction=Jurisdiction.SAUDI_ARABIA,
        auth=ADMIN_USER,
        context=base_ctx,
    )
    assert res_sa_missing.confidence_score == 0.0
    assert res_sa_missing.structured_data["error"] == "MISSING_INPUT"
    assert "currency" in res_sa_missing.structured_data["missing"]

    # Saudi Arabia with currency -> succeeds with SAR
    res_sa_ok = sup.process_message(
        message="Audit travel expense claim",
        jurisdiction=Jurisdiction.SAUDI_ARABIA,
        auth=ADMIN_USER,
        context={**base_ctx, "currency": "SAR"},
    )
    assert res_sa_ok.confidence_score > 0.0
    assert res_sa_ok.structured_data.get("currency") == "SAR"

    # 4. US without currency -> MISSING_INPUT with currency
    res_us_missing = sup.process_message(
        message="Audit travel expense claim",
        jurisdiction=Jurisdiction.USA,
        auth=ADMIN_USER,
        context=base_ctx,
    )
    assert res_us_missing.confidence_score == 0.0
    assert res_us_missing.structured_data["error"] == "MISSING_INPUT"
    assert "currency" in res_us_missing.structured_data["missing"]

    # US with currency -> succeeds with USD
    res_us_ok = sup.process_message(
        message="Audit travel expense claim",
        jurisdiction=Jurisdiction.USA,
        auth=ADMIN_USER,
        context={**base_ctx, "currency": "USD"},
    )
    assert res_us_ok.confidence_score > 0.0
    assert res_us_ok.structured_data.get("currency") == "USD"
