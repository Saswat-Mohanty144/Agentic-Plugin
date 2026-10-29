"""Regression tests for the agy review of piece I (findings F2-F11; F1 is the ADR text)."""

import copy

import httpx
import pytest
import yaml

from hrms_plugin.integrations.declarative import PROFILE_DIR, ProfileError, parse_profile
from hrms_plugin.integrations.host_registry import HostConfig, HostRegistryError
from tests.unit.test_declarative_connector import (
    ENV,
    FRAPPE_HOST,
    ICEIPTS_HOST,
    Http,
    connector,
    frappe_row,
    host,
    iceipts_page,
    iceipts_row,
)


def raw(vendor):
    return copy.deepcopy(yaml.safe_load((PROFILE_DIR / f"{vendor}.yaml").read_text()))


# F2 — headcount must be declared int; other canonical fields must be str ----------------------------------------


def test_headcount_without_int_type_is_rejected():
    data = raw("frappe")
    data["entities"]["REQUISITION"]["fields"]["headcount"].pop("type")
    with pytest.raises(ProfileError):
        parse_profile(data)


def test_text_field_declared_int_is_rejected():
    data = raw("frappe")
    data["entities"]["REQUISITION"]["fields"]["title"]["type"] = "int"
    with pytest.raises(ProfileError):
        parse_profile(data)


# F3 — strict page size ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("size", [True, "50", 50.0])
def test_page_size_is_strict_int(size):
    data = raw("frappe")
    data["entities"]["REQUISITION"]["pagination"]["size"] = size
    with pytest.raises(ProfileError):
        parse_profile(data)


# F11 — query parameter names cannot collide -------------------------------------------------------------------


@pytest.mark.parametrize("param,value", [("start_param", "fields"), ("size_param", "fields"),
                                         ("start_param", "limit_page_length")])
def test_query_parameter_names_must_be_distinct(param, value):
    data = raw("frappe")
    data["entities"]["REQUISITION"]["pagination"][param] = value
    with pytest.raises(ProfileError):
        parse_profile(data)


def test_page_param_names_must_be_distinct():
    data = raw("iceipts")
    data["entities"]["REQUISITION"]["pagination"]["size_param"] = "page"
    with pytest.raises(ProfileError):
        parse_profile(data)


# F4/F5 — vendor guards on credential accessors -------------------------------------------------------------------


def test_frappe_credentials_refused_for_iceipts_host_with_vendor_reason(tmp_path):
    with pytest.raises(HostRegistryError, match="not a frappe host"):
        host(tmp_path, ICEIPTS_HOST).credentials(ENV)


def test_bearer_token_refused_for_frappe_host_with_vendor_reason(tmp_path):
    with pytest.raises(HostRegistryError, match="not an iceipts host"):
        host(tmp_path, FRAPPE_HOST).bearer_token({"ANY": "x"})


# F6/F7 — iCeipts mapping uses every documented canonical field; lifecycle kept ------------------------------------


async def test_iceipts_department_and_manager_are_mapped(tmp_path):
    row = iceipts_row(department={"name": "DevOps", "code": "DO0023"},
                      managerId="5bcb9892-6217-4563-a3b3-97d9fdd0e44b")
    result = await connector(tmp_path, ICEIPTS_HOST, Http(iceipts_page([row]))).list_entity("REQUISITION")
    assert result.records[0]["department"] == "DevOps"
    assert result.records[0]["hiring_manager_ref"] == "5bcb9892-6217-4563-a3b3-97d9fdd0e44b"


async def test_iceipts_missing_department_is_none(tmp_path):
    result = await connector(tmp_path, ICEIPTS_HOST, Http(iceipts_page([iceipts_row()]))).list_entity("REQUISITION")
    assert result.records[0]["department"] is None


async def test_iceipts_non_string_department_name_rejects_record(tmp_path):
    row = iceipts_row(department={"name": 7})
    result = await connector(tmp_path, ICEIPTS_HOST, Http(iceipts_page([row]))).list_entity("REQUISITION")
    assert result.rejected == [{"id": "r-1", "error": "INVALID_VALUE", "field": "department"}]


@pytest.mark.parametrize("vendor_status,canonical", [("APPROVED", "APPROVED"), ("JOB_CONVERTED", "OPEN")])
async def test_approved_and_published_stay_distinct(tmp_path, vendor_status, canonical):
    http = Http(iceipts_page([iceipts_row(status=vendor_status)]))
    result = await connector(tmp_path, ICEIPTS_HOST, http).list_entity("REQUISITION")
    assert result.records[0]["status"] == canonical


# F9 — RFC 4122 allows upper-case hex ---------------------------------------------------------------------------


def test_upper_case_service_user_id_is_accepted_and_normalised():
    cfg = HostConfig(**{**ICEIPTS_HOST, "service_user_id": "EB6DF26E-E0D3-4D10-8714-873FEF6E213C"})
    assert cfg.service_user_id == "eb6df26e-e0d3-4d10-8714-873fef6e213c"


# F8/F10 — endpoint with a Frappe host; invalid profile -----------------------------------------------------------


@pytest.fixture
def frappe_env(tmp_path, monkeypatch):
    path = tmp_path / "hosts.yaml"
    path.write_text(yaml.safe_dump({"tenants": [FRAPPE_HOST]}))
    monkeypatch.setenv("HRMS_HOSTS", str(path))
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    return monkeypatch


def test_endpoint_with_frappe_host(make_client, bearer, frappe_env):
    from hrms_plugin.integrations import declarative

    http = Http({"data": [frappe_row()]})
    frappe_env.setattr(declarative, "_default_transport", lambda: httpx.MockTransport(http.handler))
    res = make_client().get("/v1/hosts/requisitions", headers=bearer(roles=["recruiter"]))
    assert res.status_code == 200
    assert res.json()["vendor"] == "frappe" and res.json()["requisitions"][0]["id"] == "HR-HIREQ-00001"
    assert http.requests[0].headers["authorization"] == "token k1:s1"


def test_endpoint_with_invalid_profile_is_500_without_detail(make_client, bearer, frappe_env):
    from hrms_plugin.integrations import declarative

    def broken(_vendor):
        raise ProfileError("secret detail")

    frappe_env.setattr(declarative, "load_profile", broken)
    res = make_client().get("/v1/hosts/requisitions", headers=bearer(roles=["recruiter"]))
    assert res.status_code == 500 and res.json() == {"error": "PROFILE_INVALID"}
