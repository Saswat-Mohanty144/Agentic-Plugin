"""Second vendor by configuration: declarative profiles, Frappe + iCeipts requisitions (piece I: I1-I7)."""

import copy
import json

import httpx
import pytest
import yaml

from hrms_plugin.integrations.declarative import (
    ConnectorError,
    DeclarativeConnector,
    ProfileError,
    load_profile,
    parse_profile,
)
from hrms_plugin.integrations.host_registry import HostRegistryError, load_host_registry

USER = "eb6df26e-e0d3-4d10-8714-873fef6e213c"  # example uuid from the iCeipts swagger
ENV = {"ACME_KEY": "k1", "ACME_SECRET": "s1", "GLOBEX_TOKEN": "jwt.token.value"}
FRAPPE_HOST = {"tenant_id": "acme", "vendor": "frappe", "base_url": "https://hr.acme.test",
               "api_key_env": "ACME_KEY", "api_secret_env": "ACME_SECRET", "timeout_seconds": 10}
ICEIPTS_HOST = {"tenant_id": "globex", "vendor": "iceipts", "base_url": "https://api.iceipts.test",
                "token_env": "GLOBEX_TOKEN", "service_user_id": USER, "timeout_seconds": 10}


def registry(tmp_path, *hosts):
    path = tmp_path / "hosts.yaml"
    path.write_text(yaml.safe_dump({"tenants": list(hosts)}))
    return load_host_registry(path)


def host(tmp_path, entry):
    return registry(tmp_path, entry).tenants[0]


class Http:
    def __init__(self, *pages, status=200):
        self.pages = list(pages)
        self.status = status
        self.requests = []

    def handler(self, request):
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"exc": "boom"})
        return httpx.Response(200, json=self.pages.pop(0))


def connector(tmp_path, entry, http):
    return DeclarativeConnector(
        host(tmp_path, entry), load_profile(entry["vendor"]), env=ENV,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(http.handler)),
    )


def frappe_row(name="HR-HIREQ-00001", **over):
    row = {"name": name, "designation": "Payroll Analyst", "department": "Finance - AC", "no_of_positions": 2,
           "status": "Open & Approved", "description": "<p>Run payroll</p>", "requested_by": "HR-EMP-00003"}
    row.update(over)
    return row


def iceipts_row(rid="r-1", **over):
    row = {"id": rid, "title": "Backend Engineer", "status": "APPROVED", "location": "Pune", "position": "3",
           "jobDescription": "Build APIs", "companyName": "Globex"}
    row.update(over)
    return row


def iceipts_page(rows, total_pages=1, page=1):
    return {"success": True, "status": 200, "data": {"pageData": rows, "totalPages": total_pages,
                                                      "currentPage": page, "totalrows": len(rows)}}


# Profiles (I1) ---------------------------------------------------------------------------------------------


def test_shipped_profiles_load():
    assert load_profile("frappe").vendor == "frappe"
    assert load_profile("iceipts").vendor == "iceipts"


def test_unknown_vendor_profile():
    with pytest.raises(ProfileError):
        load_profile("../frappe")


def _iceipts_raw():
    from hrms_plugin.integrations.declarative import PROFILE_DIR

    return yaml.safe_load((PROFILE_DIR / "iceipts.yaml").read_text())


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(extra=1),
        lambda d: d.update(auth="basic"),
        lambda d: d["entities"]["REQUISITION"].update(path="/job/{other}"),
        lambda d: d["entities"]["REQUISITION"].update(path="/job/{tenant_id}"),
        lambda d: d["entities"]["REQUISITION"]["fields"]["status"].pop("map"),
        lambda d: d["entities"]["REQUISITION"]["fields"]["status"]["map"].update(DRAFT="SOMETHING"),
        lambda d: d["entities"]["REQUISITION"]["fields"].update(salary={"from": "salary"}),
        lambda d: d["entities"]["REQUISITION"]["fields"]["title"].update(**{"from": "title;drop"}),
        lambda d: d["entities"]["REQUISITION"]["pagination"].update(size=0),
        lambda d: d["entities"]["REQUISITION"]["pagination"].update(size=101),
        lambda d: d["entities"].update(PAYSLIP=d["entities"]["REQUISITION"]),
        lambda d: d["entities"]["REQUISITION"]["fields"]["headcount"].update(type="float"),
    ],
)
def test_invalid_profiles_rejected(mutate):
    data = copy.deepcopy(_iceipts_raw())
    mutate(data)
    with pytest.raises(ProfileError):
        parse_profile(data)


def test_requested_fields_must_be_top_level():
    from hrms_plugin.integrations.declarative import PROFILE_DIR

    data = yaml.safe_load((PROFILE_DIR / "frappe.yaml").read_text())
    data["entities"]["REQUISITION"]["fields"]["title"]["from"] = "a.b"
    with pytest.raises(ProfileError):
        parse_profile(data)


# Host registry (I6) ---------------------------------------------------------------------------------------------


def test_both_vendors_in_one_registry(tmp_path):
    reg = registry(tmp_path, FRAPPE_HOST, ICEIPTS_HOST)
    assert [t.vendor for t in reg.tenants] == ["frappe", "iceipts"]


@pytest.mark.parametrize(
    "entry",
    [
        {**ICEIPTS_HOST, "token_env": None},
        {k: v for k, v in ICEIPTS_HOST.items() if k != "service_user_id"},
        {**ICEIPTS_HOST, "service_user_id": "not-a-uuid"},
        {**ICEIPTS_HOST, "api_key_env": "ACME_KEY"},
        {**ICEIPTS_HOST, "writable_fields": {"Employee": {"x": "int"}}},
        {k: v for k, v in FRAPPE_HOST.items() if k != "api_secret_env"},
        {**FRAPPE_HOST, "token_env": "GLOBEX_TOKEN"},
        {**FRAPPE_HOST, "vendor": "zoho"},
    ],
)
def test_vendor_specific_host_fields(tmp_path, entry):
    entry = {k: v for k, v in entry.items() if v is not None}
    with pytest.raises(HostRegistryError):
        registry(tmp_path, entry)


def test_iceipts_host_has_no_frappe_credentials(tmp_path):
    with pytest.raises(HostRegistryError):
        host(tmp_path, ICEIPTS_HOST).credentials(ENV)


# Frappe through the profile -------------------------------------------------------------------------------------


async def test_frappe_request_shape_and_mapping(tmp_path):
    http = Http({"data": [frappe_row()]})
    result = await connector(tmp_path, FRAPPE_HOST, http).list_entity("REQUISITION")
    req = http.requests[0]
    assert req.url.path == "/api/resource/Job Requisition"
    assert req.headers["authorization"] == "token k1:s1"
    assert req.url.params["limit_start"] == "0" and req.url.params["limit_page_length"] == "100"
    assert set(json.loads(req.url.params["fields"])) == {
        "name", "designation", "department", "no_of_positions", "status", "description", "requested_by"}
    assert result.records == [{
        "id": "HR-HIREQ-00001", "title": "Payroll Analyst", "department": "Finance - AC", "location": None,
        "headcount": 2, "status": "OPEN", "description": "<p>Run payroll</p>", "hiring_manager_ref": "HR-EMP-00003",
    }]
    assert result.rejected == []


@pytest.mark.parametrize(
    "vendor_status,canonical",
    [("Pending", "PENDING"), ("Open & Approved", "OPEN"), ("Rejected", "REJECTED"), ("Filled", "FILLED"),
     ("On Hold", "ON_HOLD"), ("Cancelled", "CANCELLED")],
)
async def test_frappe_status_vocabulary(tmp_path, vendor_status, canonical):
    http = Http({"data": [frappe_row(status=vendor_status)]})
    result = await connector(tmp_path, FRAPPE_HOST, http).list_entity("REQUISITION")
    assert result.records[0]["status"] == canonical


async def test_unmapped_value_rejects_only_that_record(tmp_path):
    http = Http({"data": [frappe_row("A", status="Archived"), frappe_row("B")]})
    result = await connector(tmp_path, FRAPPE_HOST, http).list_entity("REQUISITION")
    assert [r["id"] for r in result.records] == ["B"]
    assert result.rejected == [{"id": "A", "error": "UNMAPPED_VALUE", "field": "status"}]


async def test_frappe_offset_pagination_stops_on_short_page(tmp_path):
    http = Http({"data": [frappe_row(f"R{i}") for i in range(100)]}, {"data": [frappe_row("LAST")]})
    result = await connector(tmp_path, FRAPPE_HOST, http).list_entity("REQUISITION")
    assert len(result.records) == 101
    assert [r.url.params["limit_start"] for r in http.requests] == ["0", "100"]


async def test_frappe_exact_full_page_then_empty(tmp_path):
    http = Http({"data": [frappe_row(f"R{i}") for i in range(100)]}, {"data": []})
    result = await connector(tmp_path, FRAPPE_HOST, http).list_entity("REQUISITION")
    assert len(result.records) == 100 and len(http.requests) == 2


async def test_page_cap_fails_loudly(tmp_path):
    full = {"data": [frappe_row(f"R{i}") for i in range(100)]}
    http = Http(*[copy.deepcopy(full) for _ in range(51)])
    with pytest.raises(ConnectorError) as exc:
        await connector(tmp_path, FRAPPE_HOST, http).list_entity("REQUISITION")
    assert exc.value.code == "TOO_MANY_PAGES" and len(http.requests) == 50


async def test_non_200_is_host_error(tmp_path):
    with pytest.raises(ConnectorError) as exc:
        await connector(tmp_path, FRAPPE_HOST, Http(status=500)).list_entity("REQUISITION")
    assert exc.value.code == "HOST_ERROR"


@pytest.mark.parametrize("body", [{"data": {"x": 1}}, {"rows": []}, [], {"data": ["not-an-object"]}])
async def test_unexpected_shape_is_inconsistent_payload(tmp_path, body):
    with pytest.raises(ConnectorError) as exc:
        await connector(tmp_path, FRAPPE_HOST, Http(body)).list_entity("REQUISITION")
    assert exc.value.code == "INCONSISTENT_PAYLOAD"


async def test_missing_credentials(tmp_path):
    conn = DeclarativeConnector(host(tmp_path, FRAPPE_HOST), load_profile("frappe"), env={})
    with pytest.raises(ConnectorError) as exc:
        await conn.list_entity("REQUISITION")
    assert exc.value.code == "HOST_NOT_CONFIGURED"


async def test_profile_must_match_host_vendor(tmp_path):
    with pytest.raises(ConnectorError) as exc:
        DeclarativeConnector(host(tmp_path, FRAPPE_HOST), load_profile("iceipts"), env=ENV)
    assert exc.value.code == "VENDOR_MISMATCH"


async def test_unknown_entity(tmp_path):
    with pytest.raises(ConnectorError) as exc:
        await connector(tmp_path, FRAPPE_HOST, Http()).list_entity("PAYSLIP")
    assert exc.value.code == "ENTITY_NOT_SUPPORTED"


# iCeipts through the profile ------------------------------------------------------------------------------------


async def test_iceipts_request_shape_and_mapping(tmp_path):
    http = Http(iceipts_page([iceipts_row()]))
    result = await connector(tmp_path, ICEIPTS_HOST, http).list_entity("REQUISITION")
    req = http.requests[0]
    assert req.url.path == f"/job/getAllRequisition/{USER}"
    assert req.headers["authorization"] == "Bearer jwt.token.value"
    assert req.url.params["page"] == "1" and req.url.params["size"] == "100"
    assert "fields" not in req.url.params
    assert result.records == [{
        "id": "r-1", "title": "Backend Engineer", "department": None, "location": "Pune", "headcount": 3,
        "status": "APPROVED", "description": "Build APIs", "hiring_manager_ref": None,
    }]


@pytest.mark.parametrize(
    "vendor_status,canonical",
    [("DRAFT", "DRAFT"), ("PENDING_APPROVAL", "PENDING"), ("APPROVED", "APPROVED"), ("REJECTED", "REJECTED"),
     ("JOB_CONVERTED", "OPEN"), ("CLOSED", "CLOSED")],
)
async def test_iceipts_status_vocabulary(tmp_path, vendor_status, canonical):
    http = Http(iceipts_page([iceipts_row(status=vendor_status)]))
    result = await connector(tmp_path, ICEIPTS_HOST, http).list_entity("REQUISITION")
    assert result.records[0]["status"] == canonical


async def test_iceipts_page_pagination(tmp_path):
    http = Http(iceipts_page([iceipts_row("a")], 2, 1), iceipts_page([iceipts_row("b")], 2, 2))
    result = await connector(tmp_path, ICEIPTS_HOST, http).list_entity("REQUISITION")
    assert [r["id"] for r in result.records] == ["a", "b"]
    assert [r.url.params["page"] for r in http.requests] == ["1", "2"]


async def test_iceipts_zero_pages(tmp_path):
    http = Http(iceipts_page([], 0))
    result = await connector(tmp_path, ICEIPTS_HOST, http).list_entity("REQUISITION")
    assert result.records == [] and len(http.requests) == 1


@pytest.mark.parametrize("bad", [{"pageData": []}, {"pageData": [], "totalPages": "2"},
                                 {"pageData": [], "totalPages": -1}, {"pageData": [], "totalPages": True}])
async def test_iceipts_bad_total_pages(tmp_path, bad):
    with pytest.raises(ConnectorError) as exc:
        await connector(tmp_path, ICEIPTS_HOST, Http({"data": bad})).list_entity("REQUISITION")
    assert exc.value.code == "INCONSISTENT_PAYLOAD"


@pytest.mark.parametrize(
    "position,expected",
    [("1", 1), ("3", 3), (4, 4), ("0", None), ("-1", None), ("3.5", None), ("", None), (True, None),
     (None, None), ("٣", None), (" 3", None)],
)
async def test_headcount_conversion(tmp_path, position, expected):
    http = Http(iceipts_page([iceipts_row(position=position)]))
    result = await connector(tmp_path, ICEIPTS_HOST, http).list_entity("REQUISITION")
    if expected is None:
        assert result.records == []
        assert result.rejected == [{"id": "r-1", "error": "INVALID_VALUE", "field": "headcount"}]
    else:
        assert result.records[0]["headcount"] == expected


async def test_record_without_id_is_rejected(tmp_path):
    http = Http(iceipts_page([iceipts_row(rid=None)]))
    result = await connector(tmp_path, ICEIPTS_HOST, http).list_entity("REQUISITION")
    assert result.records == [] and result.rejected == [{"id": None, "error": "INVALID_VALUE", "field": "id"}]


# Payroll and write-back stay Frappe-only (I6) --------------------------------------------------------------------


async def test_payroll_refuses_other_vendors(tmp_path, monkeypatch):
    from hrms_plugin.payroll.service import PayrollService
    from hrms_plugin.storage.memory_store import MemoryKeyValueStore

    path = tmp_path / "hosts.yaml"
    path.write_text(yaml.safe_dump({"tenants": [ICEIPTS_HOST]}))
    monkeypatch.setenv("HRMS_HOSTS", str(path))
    out = await PayrollService(store=MemoryKeyValueStore()).calculate_pf("globex", "HR-EMP-1", "2026-09", "IN")
    assert out == {"error": "VENDOR_NOT_SUPPORTED"}


async def test_writeback_refuses_other_vendors(tmp_path, monkeypatch):
    from hrms_plugin.security.rbac import AuthContext, UserRole
    from hrms_plugin.storage.memory_store import MemoryKeyValueStore
    from hrms_plugin.writeback.service import WritebackError, WritebackService

    path = tmp_path / "hosts.yaml"
    path.write_text(yaml.safe_dump({"tenants": [ICEIPTS_HOST]}))
    monkeypatch.setenv("HRMS_HOSTS", str(path))
    auth = AuthContext(user_id="u", tenant_id="globex", roles=frozenset({UserRole.HR_ADMIN}))
    with pytest.raises(WritebackError) as exc:
        await WritebackService(store=MemoryKeyValueStore()).propose(auth, "Employee", "E1", {"x": 1})
    assert exc.value.code == "VENDOR_NOT_SUPPORTED"


# HTTP endpoint (I7) -----------------------------------------------------------------------------------------------


@pytest.fixture
def hosts_env(tmp_path, monkeypatch):
    path = tmp_path / "hosts.yaml"
    path.write_text(yaml.safe_dump({"tenants": [{**ICEIPTS_HOST, "tenant_id": "acme"}]}))
    monkeypatch.setenv("HRMS_HOSTS", str(path))
    monkeypatch.setenv("GLOBEX_TOKEN", ENV["GLOBEX_TOKEN"])
    return monkeypatch


def _patch_transport(monkeypatch, http):
    from hrms_plugin.integrations import declarative

    monkeypatch.setattr(declarative, "_default_transport", lambda: httpx.MockTransport(http.handler))


def test_endpoint_lists_canonical_requisitions(make_client, bearer, hosts_env):
    http = Http(iceipts_page([iceipts_row(), iceipts_row("r-2", status="ARCHIVED")]))
    _patch_transport(hosts_env, http)
    res = make_client().get("/v1/hosts/requisitions", headers=bearer(roles=["recruiter"]))
    assert res.status_code == 200
    body = res.json()
    assert body["vendor"] == "iceipts" and [r["id"] for r in body["requisitions"]] == ["r-1"]
    assert body["rejected"] == [{"id": "r-2", "error": "UNMAPPED_VALUE", "field": "status"}]


def test_endpoint_requires_manage_recruitment(make_client, bearer, hosts_env):
    http = Http()
    _patch_transport(hosts_env, http)
    res = make_client().get("/v1/hosts/requisitions", headers=bearer(roles=["staff"]))
    assert res.status_code == 403 and http.requests == []


def test_endpoint_without_host_is_409(make_client, bearer, monkeypatch):
    monkeypatch.delenv("HRMS_HOSTS", raising=False)
    res = make_client().get("/v1/hosts/requisitions", headers=bearer(roles=["recruiter"]))
    assert res.status_code == 409 and res.json() == {"error": "HOST_NOT_CONFIGURED"}


def test_endpoint_host_failure_is_502(make_client, bearer, hosts_env):
    _patch_transport(hosts_env, Http(status=503))
    res = make_client().get("/v1/hosts/requisitions", headers=bearer(roles=["recruiter"]))
    assert res.status_code == 502 and res.json() == {"error": "HOST_ERROR"}


async def test_path_parameter_is_encoded_even_if_validation_were_bypassed(tmp_path):
    """Defence in depth: service_user_id is UUID-validated, but the engine still percent-encodes it."""
    from hrms_plugin.integrations.host_registry import HostConfig

    sneaky = HostConfig.model_construct(**{**host(tmp_path, ICEIPTS_HOST).__dict__, "service_user_id": "../admin?x=1"})
    http = Http(iceipts_page([]))
    conn = DeclarativeConnector(sneaky, load_profile("iceipts"), env=ENV,
                                http_client=httpx.AsyncClient(transport=httpx.MockTransport(http.handler)))
    await conn.list_entity("REQUISITION")
    assert http.requests[0].url.raw_path.startswith(b"/job/getAllRequisition/..%2Fadmin%3Fx%3D1?")
