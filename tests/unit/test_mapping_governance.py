"""Synthesised mappings are versioned proposals until a different person approves them (piece G4)."""

import pytest

from hrms_plugin.schema.canonical import EntityType
from hrms_plugin.schema.introspector import DiscoveredEntity, DiscoveredField
from hrms_plugin.schema.synthesizer import MappingSynthesizer
from hrms_plugin.storage.mapping_store import MappingGovernanceError, MappingStore
from hrms_plugin.storage.memory_store import MemoryKeyValueStore
from tests.support.auth import NOW


def mapping(extra_field="work_email"):
    discovered = DiscoveredEntity(
        name="Employee",
        fields={
            "staff_id": DiscoveredField(name="staff_id", path="staff_id"),
            extra_field: DiscoveredField(name=extra_field, path=extra_field),
        },
    )
    m, _ = MappingSynthesizer.synthesize(discovered, EntityType.EMPLOYEE, vendor="zoho")
    return m


@pytest.fixture
def store():
    return MappingStore(backend=MemoryKeyValueStore())


def test_proposal_is_not_active(store):
    v = store.propose_mapping("acme", mapping(), proposed_by="a")
    assert v == 1 and store.load_mapping("acme", "zoho", "EMPLOYEE") is None
    assert store.list_versions("acme")[0]["status"] == "PROPOSED"


def test_approval_by_another_person_activates(store):
    v = store.propose_mapping("acme", mapping(), proposed_by="a")
    store.approve_version("acme", "zoho", "EMPLOYEE", v, approver="b")
    assert store.load_mapping("acme", "zoho", "EMPLOYEE") is not None
    assert store.list_versions("acme")[0]["status"] == "ACTIVE"


def test_self_approval_refused(store):
    v = store.propose_mapping("acme", mapping(), proposed_by="a")
    with pytest.raises(MappingGovernanceError) as info:
        store.approve_version("acme", "zoho", "EMPLOYEE", v, approver="a")
    assert info.value.code == "SAME_PERSON" and store.load_mapping("acme", "zoho", "EMPLOYEE") is None


def test_second_approval_supersedes_previous(store):
    v1 = store.propose_mapping("acme", mapping(), proposed_by="a")
    store.approve_version("acme", "zoho", "EMPLOYEE", v1, approver="b")
    v2 = store.propose_mapping("acme", mapping("emp_mail"), proposed_by="a")
    assert v2 == 2
    store.approve_version("acme", "zoho", "EMPLOYEE", v2, approver="b")
    statuses = {r["version"]: r["status"] for r in store.list_versions("acme")}
    assert statuses == {1: "SUPERSEDED", 2: "ACTIVE"}


def test_approving_active_version_is_idempotent(store):
    v = store.propose_mapping("acme", mapping(), proposed_by="a")
    first = store.approve_version("acme", "zoho", "EMPLOYEE", v, approver="b")
    assert store.approve_version("acme", "zoho", "EMPLOYEE", v, approver="c") == first


def test_superseded_version_cannot_be_reactivated_without_new_proposal(store):
    v1 = store.propose_mapping("acme", mapping(), proposed_by="a")
    store.approve_version("acme", "zoho", "EMPLOYEE", v1, approver="b")
    v2 = store.propose_mapping("acme", mapping("emp_mail"), proposed_by="a")
    store.approve_version("acme", "zoho", "EMPLOYEE", v2, approver="b")
    with pytest.raises(MappingGovernanceError) as info:
        store.approve_version("acme", "zoho", "EMPLOYEE", v1, approver="b")
    assert info.value.code == "INVALID_STATE"


@pytest.mark.parametrize("version", [0, 99])
def test_unknown_version(store, version):
    store.propose_mapping("acme", mapping(), proposed_by="a")
    with pytest.raises(MappingGovernanceError) as info:
        store.approve_version("acme", "zoho", "EMPLOYEE", version, approver="b")
    assert info.value.code == "NOT_FOUND"


def test_versions_are_tenant_scoped(store):
    v = store.propose_mapping("acme", mapping(), proposed_by="a")
    with pytest.raises(MappingGovernanceError):
        store.approve_version("globex", "zoho", "EMPLOYEE", v, approver="b")
    assert store.list_versions("globex") == []


# HTTP -------------------------------------------------------------------------------------------------


def _synth(client, headers):
    body = {"domain": "EMPLOYEE", "discovered_fields": [{"name": "staff_id"}, {"name": "work_email"}]}
    return client.post("/v1/schema/synthesize", json=body, headers=headers)


def test_http_synthesize_proposes_then_other_admin_approves(make_client, bearer):
    client = make_client()
    res = _synth(client, bearer(roles=["platform_admin"], sub="adm-1"))
    assert res.status_code == 200 and res.json()["mapping_status"] == "PROPOSED"
    version, vendor = res.json()["mapping_version"], res.json()["vendor"]
    url = f"/v1/schema/mappings/{vendor}/EMPLOYEE"
    assert client.get(url, headers=bearer(roles=["platform_admin"])).status_code == 404  # not active yet
    approve = f"{url}/versions/{version}/approve"
    assert client.post(approve, headers=bearer(roles=["platform_admin"], sub="adm-1")).status_code == 403
    assert client.post(approve, headers=bearer(roles=["platform_admin"], sub="adm-2", iat=NOW - 301)).status_code == 401
    ok = client.post(approve, headers=bearer(roles=["platform_admin"], sub="adm-2"))
    assert ok.status_code == 200 and ok.json()["status"] == "ACTIVE"
    assert client.get(url, headers=bearer(roles=["platform_admin"])).status_code == 200
    listing = client.get("/v1/schema/mappings", headers=bearer(roles=["platform_admin"])).json()
    assert any(v["version"] == version and v["status"] == "ACTIVE" for v in listing["versions"])


def test_http_approve_unknown_version_404(make_client, bearer):
    res = make_client().post(
        "/v1/schema/mappings/zoho/EMPLOYEE/versions/999/approve", headers=bearer(roles=["platform_admin"])
    )
    assert res.status_code == 404
