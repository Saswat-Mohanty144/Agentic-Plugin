"""QD (Q3/Q7): schema inspection returns structure only; mapped fields carry a data classification.

Unclassified fields count as PII and are not LLM-visible until a human classifies them at approval. PII and
compensation can never be marked LLM-visible.
"""

import pytest

from hrms_plugin.schema.mapping import EntityMapping, FieldMap
from hrms_plugin.server import app as app_module
from hrms_plugin.storage.mapping_store import MappingGovernanceError, MappingStore
from hrms_plugin.storage.memory_store import MemoryKeyValueStore


def mapping():
    return EntityMapping(vendor="zoho", entity_type="EMPLOYEE", id_path="emp_code", fields=(
        FieldMap(target="employee_id", source="emp_code"), FieldMap(target="work_email", source="email")))

SAMPLES = {"Employee": [{"emp_code": "E-77", "full_name": "Ravi Kumar", "email": "ravi@acme.test", "ctc": 1800000,
                         "status": "Active"},
                        {"emp_code": "E-78", "full_name": "Asha Rao", "email": "asha@acme.test", "ctc": 2100000,
                         "status": "Active"}]}
SECRETS = ("E-77", "Ravi", "ravi@acme.test", "1800000", "Asha", "2100000")


def _targets(m):
    return [f.target for f in m.fields]


@pytest.fixture
def store():
    return MappingStore(backend=MemoryKeyValueStore())


# introspect: structure only ----------------------------------------------------------------------------------
def test_introspect_returns_structure_without_sample_values(make_client, bearer):
    res = make_client().post("/v1/schema/introspect", json={"sample_records": SAMPLES},
                             headers=bearer(roles=["platform_admin"]))
    assert res.status_code == 200
    text = res.text
    assert not any(secret in text for secret in SECRETS)
    fields = res.json()["entities"]["Employee"]["fields"]
    assert set(fields) == {"emp_code", "full_name", "email", "ctc", "status"}
    assert "raw_sample" not in res.json()["entities"]["Employee"]
    assert all("sample_values" not in f for f in fields.values())


# classification at approval ----------------------------------------------------------------------------------
def test_unclassified_fields_are_recorded_as_pii_not_visible(store):
    m = mapping()
    v = store.propose_mapping("acme", m, "alice")
    row = store.approve_version("acme", "zoho", "EMPLOYEE", v, "bob")
    assert row["classification"] == {t: {"data_class": "PII", "llm_visible": False} for t in _targets(m)}


def test_explicit_classification_is_stored(store):
    m = mapping()
    first, second = _targets(m)[:2]
    v = store.propose_mapping("acme", m, "alice")
    row = store.approve_version("acme", "zoho", "EMPLOYEE", v, "bob", classification={
        first: {"data_class": "INTERNAL", "llm_visible": True}})
    assert row["classification"][first] == {"data_class": "INTERNAL", "llm_visible": True}
    assert row["classification"][second] == {"data_class": "PII", "llm_visible": False}


@pytest.mark.parametrize("entry, code", [
    ({"data_class": "PII", "llm_visible": True}, "SENSITIVE_FIELD_LLM_VISIBLE"),
    ({"data_class": "COMPENSATION", "llm_visible": True}, "SENSITIVE_FIELD_LLM_VISIBLE"),
    ({"data_class": "SECRET", "llm_visible": False}, "INVALID_CLASSIFICATION"),
    ({"data_class": "PUBLIC", "llm_visible": "yes"}, "INVALID_CLASSIFICATION"),
    ({"data_class": "PUBLIC"}, "INVALID_CLASSIFICATION"),
])
def test_invalid_classification_refused_and_nothing_activated(store, entry, code):
    m = mapping()
    v = store.propose_mapping("acme", m, "alice")
    with pytest.raises(MappingGovernanceError) as exc:
        store.approve_version("acme", "zoho", "EMPLOYEE", v, "bob", classification={_targets(m)[0]: entry})
    assert exc.value.code == code and store.active_version("acme", "zoho", "EMPLOYEE") is None


def test_classifying_an_unmapped_field_is_refused(store):
    v = store.propose_mapping("acme", mapping(), "alice")
    with pytest.raises(MappingGovernanceError) as exc:
        store.approve_version("acme", "zoho", "EMPLOYEE", v, "bob",
                              classification={"not_a_field": {"data_class": "PUBLIC", "llm_visible": True}})
    assert exc.value.code == "CLASSIFICATION_UNKNOWN_FIELD"


@pytest.mark.parametrize("data_class", ["INTERNAL", "PUBLIC"])
def test_non_sensitive_classes_may_be_visible(store, data_class):
    m = mapping()
    v = store.propose_mapping("acme", m, "alice")
    row = store.approve_version("acme", "zoho", "EMPLOYEE", v, "bob",
                                classification={_targets(m)[0]: {"data_class": data_class, "llm_visible": True}})
    assert row["status"] == "ACTIVE"


# HTTP --------------------------------------------------------------------------------------------------------
def test_http_approve_with_classification(make_client, bearer, monkeypatch):
    store = MappingStore(backend=MemoryKeyValueStore())
    monkeypatch.setattr(app_module, "mapping_store", store)
    m = mapping()
    v = store.propose_mapping("acme", m, "alice")
    target = _targets(m)[0]
    client = make_client()
    url = f"/v1/schema/mappings/zoho/EMPLOYEE/versions/{v}/approve"
    bad = client.post(url, json={"classification": {target: {"data_class": "PII", "llm_visible": True}}},
                      headers=bearer(roles=["platform_admin"], sub="bob"))
    assert bad.status_code == 422 and bad.json()["error"] == "SENSITIVE_FIELD_LLM_VISIBLE"
    ok = client.post(url, json={"classification": {target: {"data_class": "PUBLIC", "llm_visible": True}}},
                     headers=bearer(roles=["platform_admin"], sub="bob"))
    assert ok.status_code == 200 and ok.json()["classification"][target]["data_class"] == "PUBLIC"
    listing = client.get("/v1/schema/mappings", headers=bearer(roles=["platform_admin"])).json()
    assert listing["versions"][0]["classification"][target]["llm_visible"] is True


def test_http_approve_without_body_defaults_to_pii(make_client, bearer, monkeypatch):
    store = MappingStore(backend=MemoryKeyValueStore())
    monkeypatch.setattr(app_module, "mapping_store", store)
    v = store.propose_mapping("acme", mapping(), "alice")
    res = make_client().post(f"/v1/schema/mappings/zoho/EMPLOYEE/versions/{v}/approve",
                             headers=bearer(roles=["platform_admin"], sub="bob"))
    assert res.status_code == 200
    assert {c["data_class"] for c in res.json()["classification"].values()} == {"PII"}
