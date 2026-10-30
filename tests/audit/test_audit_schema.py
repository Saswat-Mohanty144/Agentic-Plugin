"""Adversarial audit tests for schema synthesis, introspection, drift, review, and approval routes.

Covers:
- End-to-end: introspect -> synthesize -> coverage -> review -> approve
- Gates: REVIEW_REQUIRED, PROBE_FAILED, SAME_PERSON, accept_suggested_classification
- BUG-1 (HIGH): Introspect omits missing expected field drift (inspect_response lacks check_missing/expected_fields)
- BUG-3 (MEDIUM): Synthesize crashes with 500 KeyError when discovered_field lacks 'name'
- BUG-4 (MEDIUM): Introspect and get_mapping return raw HTTPException {"detail": ...} instead of {"error": ...}
- Q1: Refusal of non-canonical entity domains in synthesis
"""

import pytest

from hrms_plugin.schema.mapping import EntityMapping, FieldMap
from hrms_plugin.server.app import mapping_store


@pytest.fixture
def auth_headers(bearer):
    return bearer(roles=["platform_admin"], sub="admin-1")


@pytest.fixture
def approver_headers(bearer):
    return bearer(roles=["platform_admin"], sub="approver-2")


def test_schema_e2e_introspect_synthesize_coverage_review_approve(make_client, auth_headers, approver_headers):
    """Positive E2E: introspect -> synthesize -> review -> approve."""
    client = make_client()

    # 1. Introspect sample records (sample_records is a dict of domain -> list)
    intro_resp = client.post(
        "/v1/schema/introspect",
        json={
            "vendor": "custom_hr",
            "sample_records": {
                "REQUISITION": [
                    {"req_id": "REQ-101", "job_title": "Backend Dev", "dept": "Engineering"}
                ]
            },
        },
        headers=auth_headers,
    )
    assert intro_resp.status_code == 200
    intro_data = intro_resp.json()
    assert "entities_discovered" in intro_data
    assert "REQUISITION" in intro_data["entities_discovered"]

    # 2. Synthesize mapping
    synth_resp = client.post(
        "/v1/schema/synthesize",
        json={
            "vendor": "custom_hr",
            "domain": "REQUISITION",
            "discovered_fields": [
                {"name": "req_id", "type": "string", "path": "req_id"},
                {"name": "job_title", "type": "string", "path": "job_title"},
                {"name": "dept", "type": "string", "path": "dept"},
            ],
            "sample_records": [
                {"req_id": "REQ-101", "job_title": "Backend Dev", "dept": "Engineering"}
            ],
        },
        headers=auth_headers,
    )
    assert synth_resp.status_code == 200
    synth_data = synth_resp.json()
    assert synth_data["mapping_status"] == "PROPOSED"
    version = synth_data["mapping_version"]
    assert "coverage" in synth_data

    # 3. Check version listing
    list_resp = client.get(
        "/v1/schema/mappings",
        headers=auth_headers,
    )
    assert list_resp.status_code == 200
    versions = list_resp.json()["versions"]
    assert any(v["version"] == version for v in versions)

    # 4. Review field (review produces a revised version)
    rev_resp = client.post(
        f"/v1/schema/mappings/custom_hr/requisition/versions/{version}/review",
        json={"accept": ["title"]},
        headers=auth_headers,
    )
    assert rev_resp.status_code == 200
    assert rev_resp.json()["status"] == "PROPOSED"
    revised_version = rev_resp.json()["version"]

    # 5. Approve revised mapping with different user
    appr_resp = client.post(
        f"/v1/schema/mappings/custom_hr/requisition/versions/{revised_version}/approve",
        json={"accept_suggested_classification": True},
        headers=approver_headers,
    )
    assert appr_resp.status_code == 200
    appr_data = appr_resp.json()
    assert appr_data["status"] == "ACTIVE"
    assert appr_data["approved_by"] == "approver-2"

    # 6. Verify GET active mapping returns 200
    active_resp = client.get(
        "/v1/schema/mappings/custom_hr/requisition",
        headers=auth_headers,
    )
    assert active_resp.status_code == 200
    assert active_resp.json()["vendor"] == "custom_hr"


def test_approve_gate_same_person_rejected(make_client, auth_headers):
    """Negative: same person approving their own proposed mapping must return 403 SAME_PERSON."""
    client = make_client()

    synth_resp = client.post(
        "/v1/schema/synthesize",
        json={
            "vendor": "vendor_sp",
            "domain": "REQUISITION",
            "discovered_fields": [
                {"name": "req_id", "type": "string", "path": "req_id"}
            ],
            "sample_records": [{"req_id": "R-1", "title": "Eng"}],
        },
        headers=auth_headers,
    )
    assert synth_resp.status_code == 200
    version = synth_resp.json()["mapping_version"]

    # Approve with same user (sub="admin-1")
    appr_resp = client.post(
        f"/v1/schema/mappings/vendor_sp/requisition/versions/{version}/approve",
        json={"accept_suggested_classification": True},
        headers=auth_headers,
    )
    assert appr_resp.status_code == 403
    assert appr_resp.json()["error"] == "SAME_PERSON"


def test_approve_gate_review_required(make_client, auth_headers, approver_headers):
    """Negative: version with NEEDS_REVIEW fields cannot be approved without review (409 REVIEW_REQUIRED)."""
    client = make_client()

    mapping = EntityMapping(
        vendor="vendor_rev",
        entity_type="requisition",
        id_path="job_id",
        fields=(
            FieldMap(target="id", source="job_id"),
            FieldMap(target="title", source="job_title"),
        ),
    )
    coverage = {
        "score": 0.5,
        "fields": {
            "department": {"status": "NEEDS_REVIEW", "source": None},
        },
    }
    ver = mapping_store.propose_mapping(
        tenant_id="acme",
        mapping=mapping,
        proposed_by="admin-1",
        coverage=coverage,
    )

    appr_resp = client.post(
        f"/v1/schema/mappings/vendor_rev/requisition/versions/{ver}/approve",
        json={"accept_suggested_classification": True},
        headers=approver_headers,
    )
    assert appr_resp.status_code == 409
    assert appr_resp.json()["error"] == "REVIEW_REQUIRED"
    assert "department" in appr_resp.json()["fields"]


def test_approve_gate_probe_failed(make_client, auth_headers, approver_headers):
    """Negative: version with invalid stored probe cannot be approved (409 PROBE_FAILED)."""
    client = make_client()

    mapping = EntityMapping(
        vendor="vendor_probe",
        entity_type="requisition",
        id_path="job_id",
        fields=(FieldMap(target="id", source="job_id"),),
    )
    ver = mapping_store.propose_mapping(
        tenant_id="acme",
        mapping=mapping,
        proposed_by="admin-1",
        probe={"is_valid": False, "failure_reason": "endpoint 404"},
        coverage={"score": 1.0, "fields": {}},
    )

    appr_resp = client.post(
        f"/v1/schema/mappings/vendor_probe/requisition/versions/{ver}/approve",
        json={"accept_suggested_classification": True},
        headers=approver_headers,
    )
    assert appr_resp.status_code == 409
    assert appr_resp.json()["error"] == "PROBE_FAILED"


def test_reject_version_flow(make_client, auth_headers, approver_headers):
    """Positive: rejecting a proposed version marks it REJECTED and prevents approval."""
    client = make_client()

    mapping = EntityMapping(
        vendor="vendor_rej",
        entity_type="requisition",
        id_path="job_id",
        fields=(FieldMap(target="id", source="job_id"),),
    )
    ver = mapping_store.propose_mapping(
        tenant_id="acme",
        mapping=mapping,
        proposed_by="admin-1",
        coverage={"score": 1.0, "fields": {}},
    )

    rej_resp = client.post(
        f"/v1/schema/mappings/vendor_rej/requisition/versions/{ver}/reject",
        json={"reason": "Incorrect field alignments"},
        headers=approver_headers,
    )
    assert rej_resp.status_code == 200
    assert rej_resp.json()["status"] == "REJECTED"

    appr_resp = client.post(
        f"/v1/schema/mappings/vendor_rej/requisition/versions/{ver}/approve",
        json={},
        headers=approver_headers,
    )
    assert appr_resp.status_code == 409
    assert appr_resp.json()["error"] == "INVALID_STATE"


def test_synthesize_non_canonical_domain_behavior(make_client, auth_headers):
    """Question Q1: README §12 says 'others get a spec built on the fly', but app enforces EntityType enum."""
    client = make_client()
    resp = client.post(
        "/v1/schema/synthesize",
        json={
            "vendor": "custom_hr",
            "domain": "vehicle",  # Non-canonical entity domain
            "discovered_fields": [{"name": "asset_id", "type": "string", "path": "asset_id"}],
            "sample_records": [{"asset_id": "A1"}],
        },
        headers=auth_headers,
    )
    # Documents current behavior: returns 422 INVALID_DOMAIN
    assert resp.status_code == 422
    assert resp.json()["error"] == "INVALID_DOMAIN"


# ==============================================================================
# CONFIRMED BUGS
# ==============================================================================


def test_introspect_detects_missing_expected_field_drift(make_client, auth_headers, approver_headers):
    """BUG-1 (HIGH): Introspect must detect missing expected field drift against active mapping.

    README §12 step 1: introspect runs drift detection against active mapping.
    Decision D5: SchemaDriftInterceptor inspects response and detects missing fields.
    In app.py:552-556, inspect_response() is called without check_missing=True and without expected_fields,
    causing missing expected fields to be completely ignored.
    """
    client = make_client()

    # 1. Setup an active mapping expecting "id" (mapped to "job_id") and "title" (mapped to "job_title")
    mapping = EntityMapping(
        vendor="vendor_drift",
        entity_type="requisition",
        id_path="job_id",
        fields=(
            FieldMap(target="id", source="job_id"),
            FieldMap(target="title", source="job_title"),
        ),
    )
    ver = mapping_store.propose_mapping(
        tenant_id="acme",
        mapping=mapping,
        proposed_by="admin-1",
        coverage={"score": 1.0, "fields": {}},
    )
    mapping_store.approve_version(
        tenant_id="acme",
        vendor="vendor_drift",
        entity_type="requisition",
        version=ver,
        approver="approver-2",
    )

    # 2. Introspect sample records that omit "job_title" completely
    resp = client.post(
        "/v1/schema/introspect",
        json={
            "vendor": "vendor_drift",
            "sample_records": {
                "REQUISITION": [{"job_id": "REQ-101"}]  # missing expected field "job_title"
            },
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()

    # The drift list MUST report the missing field
    assert "drift" in data
    missing_drifts = [d for d in data["drift"] if d.get("field_name") == "job_title" or "missing" in str(d).lower()]
    assert len(missing_drifts) > 0, "Expected missing field drift for 'job_title', but drift list was empty"


def test_synthesize_malformed_discovered_field_missing_name_returns_422(make_client, auth_headers):
    """BUG-3 (MEDIUM): Synthesize crashes with 500 KeyError when discovered_field lacks 'name'.

    In app.py:589, _prepare_synthesis_discovered performs `name = f['name']`.
    Because SynthesizeRequest defines discovered_fields as List[Dict[str, Any]] without item validation,
    omitting 'name' raises an unhandled KeyError returning 500 instead of 422.
    """
    client = make_client(raise_server_exceptions=False)
    resp = client.post(
        "/v1/schema/synthesize",
        json={
            "vendor": "custom_hr",
            "domain": "REQUISITION",
            "discovered_fields": [
                {"path": "raw.field", "sample_values": [123]}  # missing "name"
            ],
            "sample_records": [{"raw": {"field": 123}}],
        },
        headers=auth_headers,
    )
    assert resp.status_code == 422, f"Expected 422 Unprocessable Entity, got {resp.status_code}: {resp.text}"


def test_introspect_no_swagger_or_samples_standard_error_envelope(make_client, auth_headers):
    """BUG-4 (MEDIUM): Introspect with no swagger or samples raises HTTPException bypassing error envelope.

    README lines 827-835: all error responses return {"error": "<CODE>", "detail": ...}.
    In app.py:576, HTTPException(status_code=400, detail=...) is raised, producing {"detail": ...}.
    """
    client = make_client()
    resp = client.post(
        "/v1/schema/introspect",
        json={
            "vendor": "custom_hr",
            # neither swagger_dict nor sample_records provided
        },
        headers=auth_headers,
    )
    assert resp.status_code == 400
    data = resp.json()
    assert "error" in data, f"Expected error envelope with 'error' key, got: {data}"
    assert data["error"] == "INVALID_ARGUMENT"


def test_get_mapping_not_found_standard_error_envelope(make_client, auth_headers):
    """BUG-4 (MEDIUM): GET mapping not found raises HTTPException bypassing error envelope.

    README lines 827-835: all error responses return {"error": "<CODE>", "detail": ...}.
    In app.py:707, HTTPException(status_code=404, detail=...) is raised, producing {"detail": ...}.
    """
    client = make_client()
    resp = client.get(
        "/v1/schema/mappings/nonexistent_vendor/requisition",
        headers=auth_headers,
    )
    assert resp.status_code == 404
    data = resp.json()
    assert "error" in data, f"Expected error envelope with 'error' key, got: {data}"
    assert data["error"] == "NOT_FOUND"
