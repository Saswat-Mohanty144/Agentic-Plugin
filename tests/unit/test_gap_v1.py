"""Unit tests for GAP v1: Coverage report, review actions, reject, and approval gate."""

import pytest

from hrms_plugin.schema.canonical import EntityType
from hrms_plugin.schema.introspector import DiscoveredEntity, DiscoveredField
from hrms_plugin.schema.mapping import EntityMapping
from hrms_plugin.schema.synthesizer import MappingSynthesizer, SynthesizedFieldMap
from hrms_plugin.server import app as app_module
from hrms_plugin.storage.mapping_store import MappingGovernanceError, MappingStore
from tests.support.auth import NOW


@pytest.fixture
def mem_store():
    from hrms_plugin.storage.memory_store import MemoryKeyValueStore

    return MappingStore(backend=MemoryKeyValueStore())


def _make_discovered_with_substring():
    # 'title_role' has length >= 4 and contains 'title', triggering substring match (conf=0.75)
    return DiscoveredEntity(
        name="Requisition",
        fields={
            "id": DiscoveredField(name="id", path="id"),
            "title_role": DiscoveredField(name="title_role", path="title_role"),
            "department": DiscoveredField(name="department", path="department"),
            "unused_one": DiscoveredField(name="unused_one", path="unused_one"),
        },
    )


# 1. Coverage report tests -------------------------------------------------------------------------------------


def test_coverage_structure_and_counts():
    discovered = _make_discovered_with_substring()
    mapping, report = MappingSynthesizer.synthesize(discovered, EntityType.REQUISITION, vendor="frappe")

    assert hasattr(report, "coverage")
    cov = report.coverage
    assert "fields" in cov and "unused_host_fields" in cov and "counts" in cov
    assert "unused_one" in cov["unused_host_fields"]

    # All canonical fields for REQUISITION must appear
    fields = cov["fields"]
    assert "title" in fields
    assert "department" in fields
    assert "headcount" in fields

    # Substring match gives confidence 0.75 -> status NEEDS_REVIEW
    title_cov = fields["title"]
    assert title_cov["status"] == "NEEDS_REVIEW"
    assert title_cov["source"] == "title_role"
    assert title_cov["confidence"] == 0.75
    assert title_cov["origin"] == "RULE"
    assert title_cov["required"] is True

    # Exact match gives confidence 1.0 -> status MATCHED
    dept_cov = fields["department"]
    assert dept_cov["status"] == "MATCHED"
    assert dept_cov["source"] == "department"
    assert dept_cov["confidence"] >= 0.8
    assert dept_cov["origin"] == "RULE"

    # Unmapped canonical field -> status MISSING
    desc_cov = fields["description"]
    assert desc_cov["status"] == "MISSING"
    assert desc_cov["source"] is None
    assert desc_cov["confidence"] is None
    assert desc_cov["origin"] is None
    assert desc_cov["accepted_by"] is None

    counts = cov["counts"]
    assert counts["matched"] == sum(1 for f in fields.values() if f["status"] == "MATCHED")
    assert counts["needs_review"] == sum(1 for f in fields.values() if f["status"] == "NEEDS_REVIEW")
    assert counts["missing"] == sum(1 for f in fields.values() if f["status"] == "MISSING")
    assert counts["unused"] == len(cov["unused_host_fields"])


def test_coverage_boundary_confidence_and_llm_origin():
    # Boundary: confidence exactly 0.8 vs 0.79 with RULE origin
    rule_80 = SynthesizedFieldMap(
        canonical_target="title",
        host_source_path="pos_title",
        transform="strip",
        confidence=0.8,
        rationale="Rule match",
        origin="RULE",
        is_required=True,
    )
    rule_79 = SynthesizedFieldMap(
        canonical_target="title",
        host_source_path="pos_title",
        transform="strip",
        confidence=0.79,
        rationale="Rule match",
        origin="RULE",
        is_required=True,
    )
    # LLM origin at 0.95 confidence must still be NEEDS_REVIEW
    llm_95 = SynthesizedFieldMap(
        canonical_target="title",
        host_source_path="pos_title",
        transform="strip",
        confidence=0.95,
        rationale="LLM match",
        origin="LLM",
        is_required=True,
    )

    from hrms_plugin.schema.synthesizer import compute_coverage

    spec = {"title": {"required": True}}
    cov_80 = compute_coverage(spec, [rule_80], [])
    assert cov_80["fields"]["title"]["status"] == "MATCHED"

    cov_79 = compute_coverage(spec, [rule_79], [])
    assert cov_79["fields"]["title"]["status"] == "NEEDS_REVIEW"

    cov_llm = compute_coverage(spec, [llm_95], [])
    assert cov_llm["fields"]["title"]["status"] == "NEEDS_REVIEW"


def test_coverage_never_contains_sample_values():
    discovered = DiscoveredEntity(
        name="Employee",
        fields={
            "full_name": DiscoveredField(name="full_name", path="full_name", sample_values=["Secret Name 123"]),
            "work_email": DiscoveredField(name="work_email", path="work_email", sample_values=["secret@example.com"]),
        },
    )
    mapping, report = MappingSynthesizer.synthesize(discovered, EntityType.EMPLOYEE, vendor="zoho")
    raw_cov = str(report.coverage)
    assert "Secret Name 123" not in raw_cov
    assert "secret@example.com" not in raw_cov


# 2. HTTP Synthesis & Mapping Store Coverage Tests -------------------------------------------------------------


def test_http_synthesize_includes_coverage_and_persists_in_store(make_client, bearer, monkeypatch, mem_store):
    monkeypatch.setattr(app_module, "mapping_store", mem_store)
    client = make_client()
    admin = bearer(roles=["platform_admin"], sub="alice")

    payload = {
        "domain": "REQUISITION",
        "discovered_fields": [
            {"name": "id"},
            {"name": "title_role"},
            {"name": "department"},
            {"name": "extra_unused"},
        ],
        "vendor": "frappe",
    }
    res = client.post("/v1/schema/synthesize", json=payload, headers=admin)
    assert res.status_code == 200, res.text
    body = res.json()
    assert "coverage" in body
    assert body["coverage"]["counts"]["needs_review"] >= 1

    # Verify GET /v1/schema/mappings returns coverage and status in version rows
    list_res = client.get("/v1/schema/mappings", headers=admin)
    assert list_res.status_code == 200
    versions = list_res.json()["versions"]
    assert len(versions) == 1
    assert "coverage" in versions[0]
    assert versions[0]["status"] == "PROPOSED"


# 3. Approval Gate Tests --------------------------------------------------------------------------------------


def test_approve_refuses_when_needs_review_fields_remain(make_client, bearer, monkeypatch, mem_store):
    monkeypatch.setattr(app_module, "mapping_store", mem_store)
    client = make_client()
    alice = bearer(roles=["platform_admin"], sub="alice")
    bob = bearer(roles=["platform_admin"], sub="bob")

    payload = {
        "domain": "REQUISITION",
        "discovered_fields": [{"name": "id"}, {"name": "title_role"}, {"name": "department"}],
        "vendor": "frappe",
    }
    res = client.post("/v1/schema/synthesize", json=payload, headers=alice)
    assert res.status_code == 200
    version = res.json()["mapping_version"]

    # Bob attempts to approve a version that has NEEDS_REVIEW fields -> 409 REVIEW_REQUIRED
    approve_url = f"/v1/schema/mappings/frappe/REQUISITION/versions/{version}/approve"
    app_res = client.post(approve_url, headers=bob)
    assert app_res.status_code == 409, app_res.text
    err_body = app_res.json()
    assert err_body["error"] == "REVIEW_REQUIRED"
    assert "fields" in err_body
    assert "title" in err_body["fields"]


def test_approve_store_method_raises_review_required(mem_store):
    discovered = _make_discovered_with_substring()
    mapping, report = MappingSynthesizer.synthesize(discovered, EntityType.REQUISITION, vendor="frappe")
    v = mem_store.propose_mapping("acme", mapping, proposed_by="alice", coverage=report.coverage)

    with pytest.raises(MappingGovernanceError) as exc_info:
        mem_store.approve_version("acme", "frappe", "REQUISITION", v, approver="bob")
    assert exc_info.value.code == "REVIEW_REQUIRED"
    assert "title" in exc_info.value.fields


# 4. Reject Version Tests -------------------------------------------------------------------------------------


def test_reject_version_store_lifecycle(mem_store):
    m = EntityMapping(vendor="frappe", entity_type="REQUISITION", id_path="id")
    v = mem_store.propose_mapping("acme", m, proposed_by="alice")

    row = mem_store.reject_version(
        "acme", "frappe", "REQUISITION", v, rejected_by="bob", reason="Schema changed in production"
    )
    assert row["status"] == "REJECTED"
    assert row["reason"] == "Schema changed in production"
    assert row["rejected_by"] == "bob"
    assert "rejected_at" in row

    # Attempting to reject again -> INVALID_STATE
    with pytest.raises(MappingGovernanceError) as exc_info:
        mem_store.reject_version("acme", "frappe", "REQUISITION", v, rejected_by="bob", reason="repeat")
    assert exc_info.value.code == "INVALID_STATE"


def test_http_reject_version_flow(make_client, bearer, monkeypatch, mem_store):
    monkeypatch.setattr(app_module, "mapping_store", mem_store)
    client = make_client()
    alice = bearer(roles=["platform_admin"], sub="alice")
    bob = bearer(roles=["platform_admin"], sub="bob")

    payload = {
        "domain": "REQUISITION",
        "discovered_fields": [{"name": "id"}, {"name": "department"}],
        "vendor": "frappe",
    }
    res = client.post("/v1/schema/synthesize", json=payload, headers=alice)
    v = res.json()["mapping_version"]

    reject_url = f"/v1/schema/mappings/frappe/REQUISITION/versions/{v}/reject"

    # Auth & freshness check
    assert client.post(reject_url, headers={}, json={"reason": "bad"}).status_code == 401
    assert client.post(reject_url, headers=bearer(roles=["staff"]), json={"reason": "bad"}).status_code == 403
    stale_auth = bearer(roles=["platform_admin"], iat=NOW - 301)
    assert client.post(reject_url, headers=stale_auth, json={"reason": "bad"}).status_code == 401

    # Valid rejection
    rej_res = client.post(reject_url, headers=bob, json={"reason": "Not ready"})
    assert rej_res.status_code == 200
    assert rej_res.json()["status"] == "REJECTED"

    # Re-rejecting rejected version -> 409
    assert client.post(reject_url, headers=bob, json={"reason": "again"}).status_code == 409

    # Unknown version -> 404
    unknown_url = "/v1/schema/mappings/frappe/REQUISITION/versions/999/reject"
    assert client.post(unknown_url, headers=bob, json={"reason": "none"}).status_code == 404


# 5. Review Version Tests -------------------------------------------------------------------------------------


def test_review_validation_errors(make_client, bearer, monkeypatch, mem_store):
    monkeypatch.setattr(app_module, "mapping_store", mem_store)
    client = make_client()
    alice = bearer(roles=["platform_admin"], sub="alice")
    bob = bearer(roles=["platform_admin"], sub="bob")

    payload = {
        "domain": "REQUISITION",
        "discovered_fields": [{"name": "id"}, {"name": "title_role"}, {"name": "department"}],
        "vendor": "frappe",
    }
    res = client.post("/v1/schema/synthesize", json=payload, headers=alice)
    v = res.json()["mapping_version"]
    review_url = f"/v1/schema/mappings/frappe/REQUISITION/versions/{v}/review"

    # Problem 1: Target in more than one list (accept and reject)
    r1 = client.post(review_url, json={"accept": ["title"], "reject": ["title"]}, headers=bob)
    assert r1.status_code == 422
    assert r1.json()["error"] == "REVIEW_INVALID"
    assert any("multiple" in p.lower() for p in r1.json()["problems"])

    # Problem 2: Unknown target
    r2 = client.post(review_url, json={"accept": ["completely_unknown_field"]}, headers=bob)
    assert r2.status_code == 422
    assert r2.json()["error"] == "REVIEW_INVALID"
    assert any("unknown" in p.lower() for p in r2.json()["problems"])

    # Problem 3: Target not currently mapped in accept
    r3 = client.post(review_url, json={"accept": ["description"]}, headers=bob)
    assert r3.status_code == 422
    assert r3.json()["error"] == "REVIEW_INVALID"
    assert any("not currently mapped" in p.lower() for p in r3.json()["problems"])

    # Problem 4: Invalid transform in manual
    r4 = client.post(
        review_url,
        json={"manual": {"description": {"source": "desc", "transform": "non_existent_transform"}}},
        headers=bob,
    )
    assert r4.status_code == 422
    assert r4.json()["error"] == "REVIEW_INVALID"
    assert any("transform" in p.lower() for p in r4.json()["problems"])


def test_review_successful_lifecycle_and_four_eyes_approval(make_client, bearer, monkeypatch, mem_store):
    monkeypatch.setattr(app_module, "mapping_store", mem_store)
    client = make_client()
    alice = bearer(roles=["platform_admin"], sub="alice")
    bob = bearer(roles=["platform_admin"], sub="bob")
    carol = bearer(roles=["platform_admin"], sub="carol")

    # Alice proposes synthesized mapping with probe data
    payload = {
        "domain": "REQUISITION",
        "discovered_fields": [
            {"name": "id"},
            {"name": "title_role"},
            {"name": "department"},
            {"name": "extra_desc"},
        ],
        "sample_records": [{"id": "REQ-1", "title_role": "Lead", "department": "Eng"}],
        "vendor": "frappe",
    }
    synth_res = client.post("/v1/schema/synthesize", json=payload, headers=alice)
    assert synth_res.status_code == 200
    v1 = synth_res.json()["mapping_version"]
    assert "probe" in synth_res.json()

    # Bob reviews v1:
    # - accept "title" (moves from NEEDS_REVIEW to MATCHED with accepted_by="bob")
    # - reject "department" (removed from mapping, becomes MISSING)
    # - manual "description" with source "extra_desc" (origin MANUAL, status MATCHED, confidence null)
    review_body = {
        "accept": ["title"],
        "reject": ["department"],
        "manual": {"description": {"source": "extra_desc", "transform": "strip"}},
    }
    rev_res = client.post(
        f"/v1/schema/mappings/frappe/REQUISITION/versions/{v1}/review",
        json=review_body,
        headers=bob,
    )
    assert rev_res.status_code == 200, rev_res.text
    rev_data = rev_res.json()
    v2 = rev_data["version"]
    assert v2 == 2
    assert rev_data["status"] == "PROPOSED"
    assert rev_data["proposed_by"] == "bob"

    # Stored probe is NOT carried to revision
    assert "probe" not in rev_data

    # Verify coverage on revision v2
    v2_cov = rev_data["coverage"]
    assert v2_cov["fields"]["title"]["status"] == "MATCHED"
    assert v2_cov["fields"]["title"]["accepted_by"] == "bob"

    assert v2_cov["fields"]["department"]["status"] == "MISSING"
    assert v2_cov["fields"]["department"]["source"] is None

    assert v2_cov["fields"]["description"]["status"] == "MATCHED"
    assert v2_cov["fields"]["description"]["origin"] == "MANUAL"
    assert v2_cov["fields"]["description"]["confidence"] is None
    assert v2_cov["fields"]["description"]["source"] == "extra_desc"

    # Verify old version v1 marked REVISED with revised_into=2
    list_res = client.get("/v1/schema/mappings", headers=alice)
    v1_row = next(r for r in list_res.json()["versions"] if r["version"] == 1)
    assert v1_row["status"] == "REVISED"
    assert v1_row["revised_into"] == 2

    # Reviewing v1 again must fail with 409 INVALID_STATE
    re_rev = client.post(
        f"/v1/schema/mappings/frappe/REQUISITION/versions/{v1}/review",
        json=review_body,
        headers=bob,
    )
    assert re_rev.status_code == 409
    assert re_rev.json()["error"] == "INVALID_STATE"

    # Four-eyes on revision v2: Bob cannot approve his own revision -> 403 SAME_PERSON
    app_bob = client.post(
        f"/v1/schema/mappings/frappe/REQUISITION/versions/{v2}/approve",
        headers=bob,
    )
    assert app_bob.status_code == 403
    assert app_bob.json()["error"] == "SAME_PERSON"

    # Carol (independent admin) approves v2 -> 200 ACTIVE
    app_carol = client.post(
        f"/v1/schema/mappings/frappe/REQUISITION/versions/{v2}/approve",
        headers=carol,
    )
    assert app_carol.status_code == 200
    assert app_carol.json()["status"] == "ACTIVE"

    # Reviewing ACTIVE version v2 must fail with 409 INVALID_STATE
    app_active_rev = client.post(
        f"/v1/schema/mappings/frappe/REQUISITION/versions/{v2}/review",
        json=review_body,
        headers=alice,
    )
    assert app_active_rev.status_code == 409
    assert app_active_rev.json()["error"] == "INVALID_STATE"
