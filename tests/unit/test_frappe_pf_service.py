"""Frappe host registry, client, PF service, tool and admin endpoints (piece F1: F1-2, F1-3, F1-5, F1-6, F1-7)."""

import copy
import json

import httpx
import pytest
import yaml

from hrms_plugin.integrations.frappe import FrappeClient
from hrms_plugin.integrations.host_registry import HostRegistryError, load_host_registry
from hrms_plugin.payroll.fingerprint import slip_fingerprint
from hrms_plugin.payroll.service import PayrollService
from hrms_plugin.storage.memory_store import MemoryKeyValueStore
from tests.unit.test_pf_core import RULES_YAML, slip

HOSTS_YAML = {
    "tenants": [
        {
            "tenant_id": "acme",
            "vendor": "frappe",
            "base_url": "https://hr.acme.test",
            "api_key_env": "ACME_FRAPPE_KEY",
            "api_secret_env": "ACME_FRAPPE_SECRET",
            "timeout_seconds": 10,
        }
    ]
}
SLIP_NAME = "Sal Slip/HR-EMP-00001/00007"


class FrappeMock:
    """Frappe REST per docs.frappe.io: lists under data, documents under data, token auth header."""

    def __init__(self, slips=None, status=200):
        self.slips = [slip()] if slips is None else slips
        self.status = status
        self.requests = []

    def handler(self, request):
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"exc": "boom"})
        if request.url.path == "/api/resource/Salary Slip":
            return httpx.Response(200, json={"data": [{"name": s["name"]} for s in self.slips]})
        if request.url.path.startswith("/api/resource/Salary Slip/"):
            return httpx.Response(200, json={"data": self.slips[0]})
        return httpx.Response(404, json={})


def write(tmp_path, name, data):
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data))
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HRMS_HOSTS", str(write(tmp_path, "hosts.yaml", HOSTS_YAML)))
    monkeypatch.setenv("HRMS_PAYROLL_RULES", str(write(tmp_path, "rules.yaml", RULES_YAML)))
    monkeypatch.setenv("ACME_FRAPPE_KEY", "key123")
    monkeypatch.setenv("ACME_FRAPPE_SECRET", "secret456")
    return monkeypatch


def service(mock, store=None):
    def factory(host, key, secret):
        return FrappeClient(
            host, key, secret, http_client=httpx.AsyncClient(transport=httpx.MockTransport(mock.handler))
        )

    return PayrollService(store=store or MemoryKeyValueStore(), client_factory=factory)


async def approved_service(mock):
    svc = service(mock)
    svc.approve_fingerprint("acme", "frappe", "Salary Slip", slip_fingerprint(slip()), approved_by="admin")
    return svc


# Host registry ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,value",
    [
        ("vendor", "zoho"),
        ("base_url", "http://hr.acme.test"),
        ("api_key_env", "lower"),
        ("timeout_seconds", 0),
        ("timeout_seconds", 61),
        ("extra", 1),
    ],
)
def test_invalid_host_entries_rejected(tmp_path, field, value):
    data = copy.deepcopy(HOSTS_YAML)
    data["tenants"][0][field] = value
    with pytest.raises(HostRegistryError):
        load_host_registry(write(tmp_path, "h.yaml", data))


@pytest.mark.parametrize("value", [1, 60])
def test_timeout_boundaries(tmp_path, value):
    data = copy.deepcopy(HOSTS_YAML)
    data["tenants"][0]["timeout_seconds"] = value
    assert load_host_registry(write(tmp_path, "h.yaml", data)).for_tenant("acme").timeout_seconds == value


# Client on the wire ---------------------------------------------------------------------------------------


async def test_client_sends_token_auth_filters_and_paging(env):
    mock = FrappeMock()
    result = await (await approved_service(mock)).calculate_pf("acme", "HR-EMP-00001", "2026-09", "IN")
    assert result["employee_contribution"] == "1680"  # wage 14,000 x 12%
    listing, document = mock.requests
    assert listing.headers["authorization"] == "token key123:secret456"
    assert json.loads(listing.url.params["filters"]) == [
        ["employee", "=", "HR-EMP-00001"],
        ["start_date", ">=", "2026-09-01"],
        ["start_date", "<=", "2026-09-30"],
        ["docstatus", "=", 1],
    ]
    assert json.loads(listing.url.params["fields"]) == ["name"] and listing.url.params["limit_page_length"] == "2"
    assert document.url.raw_path.decode().endswith("/api/resource/Salary%20Slip/Sal%20Slip%2FHR-EMP-00001%2F00007")


async def test_february_bounds_in_leap_year(env):
    mock = FrappeMock()
    await (await approved_service(mock)).calculate_pf("acme", "HR-EMP-00001", "2028-02", "IN")
    assert json.loads(mock.requests[0].url.params["filters"])[2] == ["start_date", "<=", "2028-02-29"]


async def test_hostile_employee_id_stays_inside_the_json_filter(env):
    mock = FrappeMock()
    await (await approved_service(mock)).calculate_pf("acme", "HR-EMP-1 x/y", "2026-09", "IN")
    assert mock.requests[0].url.path == "/api/resource/Salary Slip"
    assert json.loads(mock.requests[0].url.params["filters"])[0] == ["employee", "=", "HR-EMP-1 x/y"]


@pytest.mark.parametrize(
    "employee_id,period",
    [
        ("", "2026-09"),
        ("x" * 141, "2026-09"),
        ('HR"EMP', "2026-09"),
        ("HR&EMP", "2026-09"),
        ("HR-EMP-1", "2026-13"),
        ("HR-EMP-1", "2026-9"),
        ("HR-EMP-1", "26-09"),
    ],
)
async def test_invalid_arguments(env, employee_id, period):
    mock = FrappeMock()
    result = await service(mock).calculate_pf("acme", employee_id, period, "IN")
    assert result["error"] == "INVALID_ARGUMENT" and mock.requests == []


# Fail closed --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("slips,code", [([], "SLIP_NOT_FOUND"), ([slip(), slip()], "AMBIGUOUS_SLIPS")])
async def test_slip_count(env, slips, code):
    assert (await (await approved_service(FrappeMock(slips))).calculate_pf("acme", "E", "2026-09", "IN"))[
        "error"
    ] == code


async def test_host_http_error(env):
    assert (await (await approved_service(FrappeMock(status=500))).calculate_pf("acme", "E", "2026-09", "IN"))[
        "error"
    ] == "HOST_ERROR"


async def test_not_applicable_outside_india(env):
    mock = FrappeMock()
    assert (await service(mock).calculate_pf("acme", "E", "2026-09", "AE"))["error"] == "NOT_APPLICABLE"
    assert mock.requests == []


@pytest.mark.parametrize("unset", ["HRMS_HOSTS", "ACME_FRAPPE_SECRET"])
async def test_host_not_configured(env, unset):
    env.delenv(unset)
    assert (await service(FrappeMock()).calculate_pf("acme", "E", "2026-09", "IN"))["error"] == "HOST_NOT_CONFIGURED"


async def test_unknown_tenant_host_not_configured(env):
    assert (await service(FrappeMock()).calculate_pf("globex", "E", "2026-09", "IN"))["error"] == "HOST_NOT_CONFIGURED"


async def test_rules_not_configured(env):
    env.delenv("HRMS_PAYROLL_RULES")
    assert (await service(FrappeMock()).calculate_pf("acme", "E", "2026-09", "IN"))["error"] == "RULES_NOT_CONFIGURED"


async def test_unapproved_fingerprint_blocks_and_reports_observed(env):
    result = await service(FrappeMock()).calculate_pf("acme", "E", "2026-09", "IN")
    assert result == {"error": "MAPPING_NOT_APPROVED", "observed_fingerprint": slip_fingerprint(slip())}


async def test_drift_quarantines_without_payload(env):
    drifted = slip()
    drifted["gross_pay"] = str(drifted["gross_pay"])  # host changed a number to a string
    store = MemoryKeyValueStore()
    svc = service(FrappeMock([drifted]), store)
    svc.approve_fingerprint("acme", "frappe", "Salary Slip", slip_fingerprint(slip()), approved_by="admin")
    result = await svc.calculate_pf("acme", "HR-EMP-00001", "2026-09", "IN")
    assert result["error"] == "SCHEMA_DRIFT" and "employee_contribution" not in result
    rows = svc.list_quarantine("acme")
    assert len(rows) == 1 and rows[0]["document"] == SLIP_NAME
    assert rows[0]["observed_fingerprint"] == slip_fingerprint(drifted)
    assert "12000" not in json.dumps(rows) and "Sunita" not in json.dumps(rows)
    assert svc.list_quarantine("globex") == []


async def test_validation_errors_surface_as_codes(env):
    bad = slip(currency="AED")
    svc = service(FrappeMock([bad]))
    svc.approve_fingerprint("acme", "frappe", "Salary Slip", slip_fingerprint(bad), approved_by="admin")
    assert (await svc.calculate_pf("acme", "E", "2026-09", "IN"))["error"] == "INCONSISTENT_PAYLOAD"


# Tool + API --------------------------------------------------------------------------------------------------------


async def test_tool_takes_ids_not_amounts_and_tenant_is_injected(env, scripted):
    from hrms_plugin.agents.orchestrator import AgenticOrchestrator
    from hrms_plugin.agents.tools import AgenticToolRegistry
    from hrms_plugin.security.rbac import AuthContext, UserRole
    from tests.support.scripted_llm import final, tool_call

    registry = AgenticToolRegistry(payroll_service=await approved_service(FrappeMock()))
    schema = registry.get_definition("calculate_pf_contribution").parameters
    assert set(schema["properties"]) == {"employee_id", "period", "jurisdiction", "tenant_id"}
    gateway, _ = scripted(
        tool_call("calculate_pf_contribution", employee_id="HR-EMP-00001", period="2026-09", tenant_id="globex"),
        final("done"),
    )
    auth = AuthContext(user_id="u", tenant_id="acme", roles=frozenset({UserRole.PAYROLL_ADMIN}))
    resp = await AgenticOrchestrator(llm_gateway=gateway, tool_registry=registry).run(
        "pf", jurisdiction="IN", auth=auth
    )
    assert resp.tool_calls[0]["arguments"]["tenant_id"] == "acme"
    assert resp.structured_data["employee_contribution"] == "1680"


async def test_employee_role_cannot_run_pf_tool(env, scripted):
    from hrms_plugin.agents.orchestrator import AgenticOrchestrator
    from hrms_plugin.agents.tools import AgenticToolRegistry
    from hrms_plugin.security.rbac import AuthContext, UserRole
    from tests.support.scripted_llm import final, tool_call

    mock = FrappeMock()
    registry = AgenticToolRegistry(payroll_service=await approved_service(mock))
    gateway, _ = scripted(tool_call("calculate_pf_contribution", employee_id="E", period="2026-09"), final("x"))
    auth = AuthContext(user_id="u", tenant_id="acme", roles=frozenset({UserRole.EMPLOYEE}))
    await AgenticOrchestrator(llm_gateway=gateway, tool_registry=registry).run("pf", jurisdiction="IN", auth=auth)
    assert mock.requests == []


def test_approve_endpoint_requires_manage_schema_and_fresh_token(make_client, bearer):
    from tests.support.auth import NOW

    body = {"vendor": "frappe", "doctype": "Salary Slip", "fingerprint": "sha256:" + "a" * 64}
    client = make_client()
    assert (
        client.post("/v1/schema/fingerprints/approve", json=body, headers=bearer(roles=["hr_manager"])).status_code
        == 403
    )
    stale = bearer(roles=["platform_admin"], iat=NOW - 301)
    assert client.post("/v1/schema/fingerprints/approve", json=body, headers=stale).status_code == 401
    ok = client.post("/v1/schema/fingerprints/approve", json=body, headers=bearer(roles=["platform_admin"], sub="adm"))
    assert ok.status_code == 200 and ok.json()["approved_by"] == "adm"


@pytest.mark.parametrize(
    "body",
    [
        {"vendor": "zoho", "doctype": "Salary Slip", "fingerprint": "sha256:" + "a" * 64},
        {"vendor": "frappe", "doctype": "Salary Slip", "fingerprint": "md5:abc"},
        {"vendor": "frappe", "doctype": "Salary Slip", "fingerprint": "sha256:" + "a" * 64, "x": 1},
    ],
)
def test_approve_endpoint_rejects_bad_bodies(make_client, bearer, body):
    assert (
        make_client()
        .post("/v1/schema/fingerprints/approve", json=body, headers=bearer(roles=["platform_admin"]))
        .status_code
        == 422
    )


def test_quarantine_listing_requires_manage_schema(make_client, bearer):
    client = make_client()
    assert client.get("/v1/schema/fingerprints/quarantine", headers=bearer(roles=["hr_manager"])).status_code == 403
    res = client.get("/v1/schema/fingerprints/quarantine", headers=bearer(roles=["platform_admin"]))
    assert res.status_code == 200 and res.json()["tenant_id"] == "acme"
