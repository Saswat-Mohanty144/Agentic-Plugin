"""QE-1 (Q2): host requisition payloads are checked against an approved shape; drift is quarantined, never mapped.

Shape = for every path the profile reads, whether the key is present and which non-null JSON types it carries.
Until a schema admin approves a shape the data is returned flagged UNAPPROVED; after approval a new type or a
vanished field is SCHEMA_DRIFT (409) and the read is quarantined.
"""

import pytest
import yaml

from hrms_plugin.integrations.declarative import observed_shape, shape_fingerprint
from hrms_plugin.server import app as app_module
from hrms_plugin.storage.mapping_store import MappingStore
from hrms_plugin.storage.memory_store import MemoryKeyValueStore
from tests.unit.test_declarative_connector import ENV, ICEIPTS_HOST, Http, _patch_transport, iceipts_page, iceipts_row

APPROVE = "/v1/hosts/requisitions/shape/approve"


@pytest.fixture
def acme(tmp_path, monkeypatch):
    path = tmp_path / "hosts.yaml"
    path.write_text(yaml.safe_dump({"tenants": [{**ICEIPTS_HOST, "tenant_id": "acme"}]}))
    monkeypatch.setenv("HRMS_HOSTS", str(path))
    monkeypatch.setenv("GLOBEX_TOKEN", ENV["GLOBEX_TOKEN"])
    monkeypatch.setattr(app_module, "mapping_store", MappingStore(backend=MemoryKeyValueStore()))
    store = MemoryKeyValueStore()
    monkeypatch.setattr(app_module, "_storage", store)
    return monkeypatch, store


def _get(make_client, bearer, monkeypatch, rows, **kw):
    _patch_transport(monkeypatch, Http(iceipts_page(rows)))
    return make_client(raise_server_exceptions=False).get("/v1/hosts/requisitions",
                                                          headers=bearer(roles=["recruiter"], **kw))


def _approve(make_client, bearer, fingerprint, roles=("platform_admin",), sub="admin"):
    return make_client().post(APPROVE, json={"fingerprint": fingerprint}, headers=bearer(roles=list(roles), sub=sub))


def _approve_fully(make_client, bearer, fingerprint, roles=("platform_admin",)):
    _approve(make_client, bearer, fingerprint, roles=roles, sub="admin1")
    return _approve(make_client, bearer, fingerprint, roles=roles, sub="admin2")


# shape -------------------------------------------------------------------------------------------------------
def test_shape_records_presence_and_non_null_types():
    from hrms_plugin.integrations.declarative import load_profile

    spec = load_profile("iceipts").entities["REQUISITION"]
    rows = [iceipts_row(department={"name": "Eng"}), iceipts_row("r-2", position=None)]
    shape = observed_shape(rows, spec)
    assert shape["position"] == {"present": True, "types": ["str"]}
    assert shape["department.name"] == {"present": True, "types": ["str"]}
    assert shape["managerId"] == {"present": False, "types": []}
    assert shape_fingerprint(shape) == shape_fingerprint(dict(reversed(list(shape.items()))))


# route -------------------------------------------------------------------------------------------------------
def test_first_read_is_flagged_unapproved_and_approval_is_by_schema_admin(make_client, bearer, acme):
    monkeypatch, _ = acme
    body = _get(make_client, bearer, monkeypatch, [iceipts_row()]).json()
    assert body["schema"]["status"] == "UNAPPROVED" and body["requisitions"]
    fp = body["schema"]["fingerprint"]
    assert _approve(make_client, bearer, fp, roles=("recruiter",)).status_code == 403
    assert _approve(make_client, bearer, "sha256:" + "0" * 64).status_code == 409  # not the observed shape
    r1 = _approve(make_client, bearer, fp, sub="admin1")
    assert r1.status_code == 202 and r1.json()["status"] == "PENDING_CONFIRMATION"
    r2 = _approve(make_client, bearer, fp, sub="admin2")
    assert r2.status_code == 200 and r2.json()["status"] == "APPROVED"
    again = _get(make_client, bearer, monkeypatch, [iceipts_row("r-9")]).json()
    assert again["schema"] == {"status": "APPROVED", "fingerprint": fp}


def test_new_type_after_approval_is_drift_and_quarantined(make_client, bearer, acme):
    monkeypatch, store = acme
    fp = _get(make_client, bearer, monkeypatch, [iceipts_row()]).json()["schema"]["fingerprint"]
    _approve_fully(make_client, bearer, fp)
    res = _get(make_client, bearer, monkeypatch, [iceipts_row(position=3)])  # "3" was a string, now an int
    assert res.status_code == 409 and res.json()["error"] == "SCHEMA_DRIFT"
    assert res.json()["changed"] == ["position"] and "requisitions" not in res.json()
    assert any(k.startswith("acme:iceipts:REQUISITION") for k in store.get_all("quarantine"))


def test_vanished_field_after_approval_is_drift(make_client, bearer, acme):
    monkeypatch, _ = acme
    fp = _get(make_client, bearer, monkeypatch, [iceipts_row()]).json()["schema"]["fingerprint"]
    _approve_fully(make_client, bearer, fp)
    row = iceipts_row()
    del row["jobDescription"]
    res = _get(make_client, bearer, monkeypatch, [row])
    assert res.status_code == 409 and res.json()["changed"] == ["jobDescription"]


def test_nulls_within_an_approved_shape_are_not_drift(make_client, bearer, acme):
    monkeypatch, _ = acme
    fp = _get(make_client, bearer, monkeypatch, [iceipts_row()]).json()["schema"]["fingerprint"]
    _approve_fully(make_client, bearer, fp)
    res = _get(make_client, bearer, monkeypatch, [iceipts_row(location=None)])
    assert res.status_code == 200 and res.json()["schema"]["status"] == "APPROVED"


def test_empty_result_has_no_shape_to_judge(make_client, bearer, acme):
    monkeypatch, _ = acme
    body = _get(make_client, bearer, monkeypatch, []).json()
    assert body["schema"] == {"status": "NO_ROWS"} and body["requisitions"] == []


def test_approval_is_per_tenant(make_client, bearer, acme):
    monkeypatch, store = acme
    fp = _get(make_client, bearer, monkeypatch, [iceipts_row()]).json()["schema"]["fingerprint"]
    _approve(make_client, bearer, fp)
    assert all(k.startswith("acme:") for k in store.get_all("approved_shapes"))
