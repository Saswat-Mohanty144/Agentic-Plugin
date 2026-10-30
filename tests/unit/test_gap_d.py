"""Tests for gap d: decisions D4 and D5 (declarative transforms, probe, drift, vendor)."""

from __future__ import annotations

import json

import pytest
import yaml
from pydantic import ValidationError

from hrms_plugin.integrations.declarative import (
    DeclarativeConnector,
    FieldSpec,
    _Rejected,
    load_profile,
)
from hrms_plugin.integrations.mapping_overlay import OverlayError, overlay
from hrms_plugin.schema.mapping import EntityMapping, FieldMap
from hrms_plugin.server import app as app_module
from hrms_plugin.storage.mapping_store import MappingGovernanceError, MappingStore
from hrms_plugin.storage.memory_store import MemoryKeyValueStore
from tests.unit.test_declarative_connector import ENV, FRAPPE_HOST, Http, _patch_transport, frappe_row

SPEC = load_profile("iceipts").entities["REQUISITION"]
FRAPPE_SPEC = load_profile("frappe").entities["REQUISITION"]


def _mapping(vendor: str = "iceipts", *fields: FieldMap) -> EntityMapping:
    return EntityMapping(vendor=vendor, entity_type="REQUISITION", id_path="id", fields=tuple(fields))


# D4: FieldSpec transforms and required ------------------------------------------------------------------------

def test_field_spec_transforms_and_required_supported():
    for tr in ("strip", "lower", "upper"):
        spec = FieldSpec(source="title", transform=tr)
        assert spec.transform == tr
    spec_req = FieldSpec(source="title", required=True)
    assert spec_req.required is True
    spec_opt = FieldSpec(source="title", required=False)
    assert spec_opt.required is False


def test_field_spec_invalid_transform_rejected():
    with pytest.raises(ValidationError):
        FieldSpec(source="title", transform="reverse")


def test_field_spec_transform_on_int_rejected():
    with pytest.raises(ValidationError):
        FieldSpec(source="headcount", type="int", transform="strip")


def test_declarative_connector_transforms_strings():
    row = {"title": "  Software Engineer  "}
    fs_strip = FieldSpec(source="title", transform="strip")
    assert DeclarativeConnector._value(row, "title", fs_strip) == "Software Engineer"
    fs_lower = FieldSpec(source="title", transform="lower")
    assert DeclarativeConnector._value(row, "title", fs_lower) == "  software engineer  "
    fs_upper = FieldSpec(source="title", transform="upper")
    assert DeclarativeConnector._value(row, "title", fs_upper) == "  SOFTWARE ENGINEER  "


def test_declarative_connector_required_field_missing_or_empty_rejected():
    fs_req = FieldSpec(source="title", required=True)
    with pytest.raises(_Rejected) as exc1:
        DeclarativeConnector._value({}, "title", fs_req)
    assert exc1.value.error == "MISSING_VALUE"

    with pytest.raises(_Rejected) as exc2:
        DeclarativeConnector._value({"title": ""}, "title", fs_req)
    assert exc2.value.error == "MISSING_VALUE"

    fs_strip_req = FieldSpec(source="title", transform="strip", required=True)
    with pytest.raises(_Rejected) as exc3:
        DeclarativeConnector._value({"title": "   "}, "title", fs_strip_req)
    assert exc3.value.error == "MISSING_VALUE"

    fs_int_req = FieldSpec(source="headcount", type="int", required=True)
    with pytest.raises(_Rejected) as exc4:
        DeclarativeConnector._value({}, "headcount", fs_int_req)
    assert exc4.value.error == "MISSING_VALUE"
    with pytest.raises(_Rejected) as exc5:
        DeclarativeConnector._value({"headcount": ""}, "headcount", fs_int_req)
    assert exc5.value.error == "MISSING_VALUE"


def test_declarative_connector_optional_field_preserves_none_or_empty():
    fs = FieldSpec(source="title", required=False)
    assert DeclarativeConnector._value({}, "title", fs) is None
    assert DeclarativeConnector._value({"title": ""}, "title", fs) == ""


# D4: Mapping overlay pass-through -----------------------------------------------------------------------------

def test_overlay_passes_transform_and_required():
    spec, overridden = overlay(
        SPEC,
        _mapping("iceipts", FieldMap(target="title", source="title", transform="strip", required=True)),
    )
    assert "title" in overridden
    assert spec.fields["title"].transform == "strip"
    assert spec.fields["title"].required is True

    for tr in ("lower", "upper"):
        s, _ = overlay(SPEC, _mapping("iceipts", FieldMap(target="title", source="title", transform=tr)))
        assert s.fields["title"].transform == tr


def test_overlay_refuses_invalid_transforms_defaults_and_types():
    with pytest.raises(OverlayError) as exc1:
        overlay(SPEC, _mapping("iceipts", FieldMap(target="title", source="title", default="Untitled")))
    assert "default" in exc1.value.reason

    with pytest.raises(OverlayError) as exc2:
        overlay(SPEC, _mapping("iceipts", FieldMap(target="title", source=("a", "b"))))
    assert "single source" in exc2.value.reason

    with pytest.raises(OverlayError) as exc3:
        overlay(SPEC, _mapping("iceipts", FieldMap(target="title", source="title", transform="reverse")))
    assert "transform" in exc3.value.reason

    with pytest.raises(OverlayError) as exc4:
        overlay(SPEC, _mapping("iceipts", FieldMap(target="title", source="title", transform="int")))
    assert "type" in exc4.value.reason

    with pytest.raises(OverlayError) as exc5:
        overlay(SPEC, _mapping("iceipts", FieldMap(target="headcount", source="position", transform="strip")))
    assert "type" in exc5.value.reason


# D4 & D5: MappingStore probe lifecycle ------------------------------------------------------------------------

def test_mapping_store_propose_and_approve_probe_lifecycle():
    store = MappingStore(backend=MemoryKeyValueStore())
    probe_ok = {"is_valid": True, "quality_score": 1.0, "records_tested": 2, "issues": []}
    fm = FieldMap(target="title", source="title")
    v1 = store.propose_mapping("acme", _mapping("iceipts", fm), "alice", probe=probe_ok)
    versions = store.list_versions("acme")
    assert versions[0]["probe"] == probe_ok

    approved = store.approve_version("acme", "iceipts", "REQUISITION", v1, "bob")
    assert approved["status"] == "ACTIVE"
    assert approved["probe"] == probe_ok

    v2 = store.propose_mapping("acme", _mapping("iceipts", fm), "alice")
    assert "probe" not in store.list_versions("acme")[1]
    assert store.approve_version("acme", "iceipts", "REQUISITION", v2, "bob")["status"] == "ACTIVE"


def test_mapping_store_approve_fails_when_probe_invalid():
    store = MappingStore(backend=MemoryKeyValueStore())
    probe_fail = {
        "is_valid": False, "quality_score": 0.0, "records_tested": 1,
        "issues": [{"severity": "ERROR", "field": "id", "message": "fail"}],
    }
    fm = FieldMap(target="title", source="title")
    v1 = store.propose_mapping("acme", _mapping("iceipts", fm), "alice", probe=probe_fail)
    with pytest.raises(MappingGovernanceError) as exc:
        store.approve_version("acme", "iceipts", "REQUISITION", v1, "bob")
    assert exc.value.code == "PROBE_FAILED"


# D4 & D5: Synthesize routes -----------------------------------------------------------------------------------

def test_synthesize_vendor_validation_and_default(make_client, bearer, monkeypatch):
    store = MappingStore(backend=MemoryKeyValueStore())
    monkeypatch.setattr(app_module, "mapping_store", store)
    client = make_client()
    auth = bearer(roles=["platform_admin"], sub="alice")

    payload = {
        "domain": "REQUISITION",
        "discovered_fields": [{"name": "designation"}, {"name": "department"}],
    }
    # Default vendor is host
    res_default = client.post("/v1/schema/synthesize", json=payload, headers=auth)
    assert res_default.status_code == 200
    assert res_default.json()["vendor"] == "host"

    # Custom vendor
    res_frappe = client.post("/v1/schema/synthesize", json={**payload, "vendor": "frappe"}, headers=auth)
    assert res_frappe.status_code == 200
    assert res_frappe.json()["vendor"] == "frappe"
    assert store.load_mapping("acme", "frappe", "REQUISITION") is None  # still proposed, not active

    # Invalid vendors -> 422
    for bad_vendor in ("Invalid Vendor!", "", "a" * 33, "vendor/sub"):
        res_bad = client.post("/v1/schema/synthesize", json={**payload, "vendor": bad_vendor}, headers=auth)
        assert res_bad.status_code == 422


def test_synthesize_with_sample_records_runs_probe(make_client, bearer, monkeypatch):
    store = MappingStore(backend=MemoryKeyValueStore())
    monkeypatch.setattr(app_module, "mapping_store", store)
    client = make_client()
    auth = bearer(roles=["platform_admin"], sub="alice")
    bob_auth = bearer(roles=["platform_admin"], sub="bob")

    samples_valid = [
        {"id": "REQ-1", "designation": "Backend Dev", "department": "Eng"},
        {"id": "REQ-2", "designation": "Frontend Dev", "department": "Design"},
    ]
    payload = {
        "domain": "REQUISITION",
        "discovered_fields": [{"name": "id"}, {"name": "designation"}, {"name": "department"}],
        "vendor": "frappe",
        "sample_records": samples_valid,
    }
    res = client.post("/v1/schema/synthesize", json=payload, headers=auth)
    assert res.status_code == 200
    data = res.json()
    assert "probe" in data
    probe = data["probe"]
    assert probe["is_valid"] is True
    assert probe["records_tested"] == 2
    assert isinstance(probe["issues"], list)

    v = data["mapping_version"]
    app_res = client.post(f"/v1/schema/mappings/frappe/REQUISITION/versions/{v}/approve", headers=bob_auth)
    assert app_res.status_code == 200

    # Max length boundary: > 50 -> 422
    samples_51 = [{"id": f"REQ-{i}", "designation": "Dev"} for i in range(51)]
    res_51 = client.post("/v1/schema/synthesize", json={**payload, "sample_records": samples_51}, headers=auth)
    assert res_51.status_code == 422

    # Exactly 50 -> 200
    samples_50 = [{"id": f"REQ-{i}", "designation": "Dev"} for i in range(50)]
    res_50 = client.post("/v1/schema/synthesize", json={**payload, "sample_records": samples_50}, headers=auth)
    assert res_50.status_code == 200
    assert res_50.json()["probe"]["records_tested"] == 50

    # Failing probe: id missing in sample records -> is_valid is False
    samples_bad = [{"designation": "Dev"}]  # no id field
    res_bad = client.post("/v1/schema/synthesize", json={**payload, "sample_records": samples_bad}, headers=auth)
    assert res_bad.status_code == 200
    v_bad = res_bad.json()["mapping_version"]
    assert res_bad.json()["probe"]["is_valid"] is False

    # Approving version whose stored probe is invalid -> 409 PROBE_FAILED
    app_fail = client.post(f"/v1/schema/mappings/frappe/REQUISITION/versions/{v_bad}/approve", headers=bob_auth)
    assert app_fail.status_code == 409
    assert app_fail.json()["error"] == "PROBE_FAILED"


# D5: Introspect with sample_records detects drift -------------------------------------------------------------

def test_introspect_schema_with_sample_records_detects_drift(make_client, bearer, monkeypatch):
    store = MappingStore(backend=MemoryKeyValueStore())
    monkeypatch.setattr(app_module, "mapping_store", store)
    client = make_client()
    auth = bearer(roles=["platform_admin"], sub="alice")

    # Set up an active mapping for vendor "frappe" entity "REQUISITION"
    v = store.propose_mapping("acme", EntityMapping(
        vendor="frappe",
        entity_type="REQUISITION",
        id_path="name",
        fields=(FieldMap(target="title", source="designation"), FieldMap(target="department", source="department")),
    ), "alice")
    store.approve_version("acme", "frappe", "REQUISITION", v, "bob")

    # Introspect with sample records containing an unmapped field
    samples = {
        "REQUISITION": [
            {"name": "HR-REQ-001", "designation": "Eng Lead", "department": "R&D", "unexpected_col": "secret123"},
        ]
    }
    res = client.post("/v1/schema/introspect", json={"sample_records": samples, "vendor": "frappe"}, headers=auth)
    assert res.status_code == 200
    data = res.json()
    assert "drift" in data
    assert len(data["drift"]) == 1
    drift_item = data["drift"][0]
    assert drift_item["field_name"] == "unexpected_col"
    assert drift_item["severity"] == "INFO"
    assert "raw_sample" not in str(data)
    assert "secret123" not in json.dumps(data)  # never return raw sample values

    # Unknown vendor or no active mapping -> drift list empty
    body_no_map = {"sample_records": samples, "vendor": "unknown_vendor"}
    res_no_map = client.post("/v1/schema/introspect", json=body_no_map, headers=auth)
    assert res_no_map.status_code == 200
    assert res_no_map.json()["drift"] == []


def test_introspect_invalid_vendor_rejected(make_client, bearer):
    client = make_client()
    auth = bearer(roles=["platform_admin"], sub="alice")
    body = {"sample_records": {"REQUISITION": []}, "vendor": "INVALID!"}
    res = client.post("/v1/schema/introspect", json=body, headers=auth)
    assert res.status_code == 422


# End-to-End flow: synthesize frappe, approve, GET /v1/hosts/requisitions applies overlay ----------------------

def test_e2e_synthesize_frappe_requisition_and_apply_override(make_client, bearer, tmp_path, monkeypatch):
    hosts_path = tmp_path / "hosts.yaml"
    hosts_path.write_text(yaml.safe_dump({"tenants": [{**FRAPPE_HOST, "tenant_id": "acme"}]}))
    monkeypatch.setenv("HRMS_HOSTS", str(hosts_path))
    monkeypatch.setenv("ACME_KEY", ENV["ACME_KEY"])
    monkeypatch.setenv("ACME_SECRET", ENV["ACME_SECRET"])

    store = MappingStore(backend=MemoryKeyValueStore())
    monkeypatch.setattr(app_module, "mapping_store", store)

    client = make_client(raise_server_exceptions=False)
    alice_token = bearer(roles=["platform_admin"], sub="USR-001")
    bob_token = bearer(roles=["platform_admin"], sub="USR-002")

    discovered_fields = [
        {"name": "name"},
        {"name": "designation"},
        {"name": "department"},
        {"name": "no_of_positions"},
        {"name": "status"},
        {"name": "description"},
        {"name": "requested_by"},
    ]
    sample_records = [
        {
            "name": "HR-HIREQ-00001",
            "designation": "Payroll Analyst",
            "department": "Finance - AC",
            "no_of_positions": 2,
            "status": "Open & Approved",
            "description": "<p>Run payroll</p>",
            "requested_by": "HR-EMP-00003",
        }
    ]

    # 1. Synthesize with vendor "frappe"
    syn_res = client.post("/v1/schema/synthesize", json={
        "domain": "REQUISITION",
        "vendor": "frappe",
        "discovered_fields": discovered_fields,
        "sample_records": sample_records,
    }, headers=alice_token)
    assert syn_res.status_code == 200
    syn_data = syn_res.json()
    version = syn_data["mapping_version"]
    assert syn_data["vendor"] == "frappe"
    assert syn_data["probe"]["is_valid"] is True

    # 2. Approve with different user (bob)
    app_res = client.post(f"/v1/schema/mappings/frappe/REQUISITION/versions/{version}/approve", headers=bob_token)
    assert app_res.status_code == 200
    assert app_res.json()["status"] == "ACTIVE"

    # 3. GET /v1/hosts/requisitions applies TENANT_OVERRIDE instead of 409 MAPPING_NOT_APPLICABLE
    _patch_transport(monkeypatch, Http({"data": [frappe_row()]}))
    recruiter_token = bearer(roles=["recruiter"])
    req_res = client.get("/v1/hosts/requisitions", headers=recruiter_token)
    assert req_res.status_code == 200
    req_data = req_res.json()
    assert req_data["mapping"]["layer"] == "TENANT_OVERRIDE"
    assert req_data["mapping"]["version"] == version
    assert len(req_data["requisitions"]) == 1
    assert req_data["requisitions"][0]["id"] == "HR-HIREQ-00001"
