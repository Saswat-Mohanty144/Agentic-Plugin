"""QC (Q1): the tenant's approved mapping is on the requisition read path; unapproved proposals never are.

Resolution order: approved tenant mapping (field by field) > vendor profile default. A pending LLM proposal changes
nothing. An approved mapping the connector cannot apply faithfully is a 409, never a silent fallback.
"""

import pytest
import yaml

from hrms_plugin.integrations.declarative import load_profile
from hrms_plugin.integrations.mapping_overlay import OverlayError, overlay
from hrms_plugin.schema.mapping import EntityMapping, FieldMap
from hrms_plugin.server import app as app_module
from hrms_plugin.storage.mapping_store import MappingStore
from hrms_plugin.storage.memory_store import MemoryKeyValueStore
from tests.unit.test_declarative_connector import ENV, ICEIPTS_HOST, Http, _patch_transport, iceipts_page, iceipts_row

SPEC = load_profile("iceipts").entities["REQUISITION"]


def mapping(*fields):
    return EntityMapping(vendor="iceipts", entity_type="REQUISITION", id_path="id", fields=tuple(fields))


# overlay -----------------------------------------------------------------------------------------------------
def test_override_replaces_only_the_named_fields():
    spec, overridden = overlay(SPEC, mapping(FieldMap(target="department", source="companyName")))
    assert spec.fields["department"].source == "companyName" and overridden == ["department"]
    assert spec.fields["title"] == SPEC.fields["title"]


def test_values_become_a_strict_status_map():
    status = FieldMap(target="status", source="status", values={"APPROVED": "OPEN", "ARCHIVED": "CLOSED"})
    spec, _ = overlay(SPEC, mapping(status))
    assert spec.fields["status"].map == {"APPROVED": "OPEN", "ARCHIVED": "CLOSED"}


def test_int_transform_for_headcount():
    spec, _ = overlay(SPEC, mapping(FieldMap(target="headcount", source="openings", transform="int")))
    assert spec.fields["headcount"].type == "int"


@pytest.mark.parametrize("field, reason", [
    (FieldMap(target="salary_band", source="band"), "not a requisition field"),
    (FieldMap(target="title", source=("title", "name")), "single source"),
    (FieldMap(target="title", source="title", transform="decimal"), "transform"),
    (FieldMap(target="title", source="title", default="Untitled"), "default"),
    (FieldMap(target="headcount", source="position"), "type"),
    (FieldMap(target="title", source="title", transform="int"), "type"),
    (FieldMap(target="status", source="status"), "status needs a value map"),
    (FieldMap(target="status", source="status", values={"APPROVED": "LIVE"}), "unknown canonical"),
    (FieldMap(target="title", source="a b"), "source"),
])
def test_unfaithful_overrides_are_refused(field, reason):
    with pytest.raises(OverlayError) as exc:
        overlay(SPEC, mapping(field))
    assert exc.value.field == field.target and reason in exc.value.reason


def test_faithful_transforms_and_required_are_accepted():
    for tr in ("strip", "lower", "upper"):
        spec, _ = overlay(SPEC, mapping(FieldMap(target="title", source="title", transform=tr)))
        assert spec.fields["title"].transform == tr
    spec_req, _ = overlay(SPEC, mapping(FieldMap(target="title", source="title", required=True)))
    assert spec_req.fields["title"].required is True


# store -------------------------------------------------------------------------------------------------------
def test_only_an_approved_version_is_active():
    store = MappingStore(backend=MemoryKeyValueStore())
    assert store.active_version("acme", "iceipts", "REQUISITION") is None
    m1 = mapping(FieldMap(target="department", source="companyName"))
    v1 = store.propose_mapping("acme", m1, "alice")
    assert store.active_version("acme", "iceipts", "REQUISITION") is None  # proposed only
    rev1 = store.review_version("acme", "iceipts", "REQUISITION", v1, "alice", actions={"accept": ["department"]})
    v1_app = rev1["version"]
    store.approve_version("acme", "iceipts", "REQUISITION", v1_app, "bob")
    version, active = store.active_version("acme", "iceipts", "REQUISITION")
    assert version == v1_app and active.fields[0].source == "companyName"
    m2 = mapping(FieldMap(target="department", source="location"))
    v2 = store.propose_mapping("acme", m2, "alice")
    assert store.active_version("acme", "iceipts", "REQUISITION")[0] == v1_app  # pending v2 changes nothing
    rev2 = store.review_version("acme", "iceipts", "REQUISITION", v2, "alice", actions={"accept": ["department"]})
    v2_app = rev2["version"]
    store.approve_version("acme", "iceipts", "REQUISITION", v2_app, "bob")
    assert store.active_version("acme", "iceipts", "REQUISITION")[0] == v2_app
    assert store.active_version("globex", "iceipts", "REQUISITION") is None


# route -------------------------------------------------------------------------------------------------------
@pytest.fixture
def acme_iceipts(tmp_path, monkeypatch):
    path = tmp_path / "hosts.yaml"
    path.write_text(yaml.safe_dump({"tenants": [{**ICEIPTS_HOST, "tenant_id": "acme"}]}))
    monkeypatch.setenv("HRMS_HOSTS", str(path))
    monkeypatch.setenv("GLOBEX_TOKEN", ENV["GLOBEX_TOKEN"])
    store = MappingStore(backend=MemoryKeyValueStore())
    monkeypatch.setattr(app_module, "mapping_store", store)
    return monkeypatch, store


def _get(make_client, bearer, monkeypatch, rows):
    _patch_transport(monkeypatch, Http(iceipts_page(rows)))
    return make_client(raise_server_exceptions=False).get("/v1/hosts/requisitions",
                                                          headers=bearer(roles=["recruiter"]))


def test_route_without_tenant_mapping_uses_the_vendor_profile(make_client, bearer, acme_iceipts):
    monkeypatch, _ = acme_iceipts
    body = _get(make_client, bearer, monkeypatch, [iceipts_row(department={"name": "Eng"})]).json()
    assert body["requisitions"][0]["department"] == "Eng"
    assert body["mapping"] == {"layer": "VENDOR_PROFILE"}


def test_route_applies_the_approved_tenant_override(make_client, bearer, acme_iceipts):
    monkeypatch, store = acme_iceipts
    m = mapping(
        FieldMap(target="department", source="companyName"),
        FieldMap(target="status", source="status", values={"APPROVED": "OPEN", "ARCHIVED": "CLOSED"}))
    v = store.propose_mapping("acme", m, "alice")
    rev = store.review_version(
        "acme", "iceipts", "REQUISITION", v, "alice", actions={"accept": ["department", "status"]}
    )
    v_app = rev["version"]
    store.approve_version("acme", "iceipts", "REQUISITION", v_app, "bob")
    rows = [iceipts_row(department={"name": "Eng"}), iceipts_row("r-2", status="ARCHIVED")]
    body = _get(make_client, bearer, monkeypatch, rows).json()
    assert [(r["department"], r["status"]) for r in body["requisitions"]] == [("Globex", "OPEN"),
                                                                             ("Globex", "CLOSED")]
    assert body["mapping"] == {"layer": "TENANT_OVERRIDE", "version": v_app, "fields": ["department", "status"]}


def test_route_ignores_a_pending_proposal(make_client, bearer, acme_iceipts):
    monkeypatch, store = acme_iceipts
    store.propose_mapping("acme", mapping(FieldMap(target="department", source="companyName")), "alice")
    body = _get(make_client, bearer, monkeypatch, [iceipts_row(department={"name": "Eng"})]).json()
    assert body["requisitions"][0]["department"] == "Eng" and body["mapping"] == {"layer": "VENDOR_PROFILE"}


def test_route_refuses_an_approved_mapping_it_cannot_apply(make_client, bearer, acme_iceipts):
    monkeypatch, store = acme_iceipts
    m = mapping(FieldMap(target="title", source="title", transform="decimal"))
    v = store.propose_mapping("acme", m, "alice")
    rev = store.review_version("acme", "iceipts", "REQUISITION", v, "alice", actions={"accept": ["title"]})
    v_app = rev["version"]
    store.approve_version("acme", "iceipts", "REQUISITION", v_app, "bob")
    res = _get(make_client, bearer, monkeypatch, [iceipts_row()])
    assert res.status_code == 409 and res.json()["error"] == "MAPPING_NOT_APPLICABLE"
    assert res.json()["field"] == "title" and res.json()["version"] == v_app
