"""Tests for Runtime Schema Drift Interceptor & Self-Healing Generator."""

from __future__ import annotations

from hrms_plugin.schema.canonical import EntityType
from hrms_plugin.schema.drift import DriftSeverity, SchemaDriftInterceptor
from hrms_plugin.schema.mapping import EntityMapping, FieldMap


def test_schema_drift_detects_unmapped_fields():
    """Verify interceptor identifies new, unmapped fields returned by host APIs."""
    interceptor = SchemaDriftInterceptor()
    mapping = EntityMapping(
        vendor="test_hrms",
        entity_type="EMPLOYEE",
        id_path="id",
        fields=(FieldMap(target="full_name", source="name"),),
    )

    raw_response = {
        "id": "emp_101",
        "name": "Jane Doe",
        "custom_tax_bracket": "Slab-3",
        "remote_stipend_eligible": True,
    }

    events = interceptor.inspect_response(EntityType.EMPLOYEE, mapping, raw_response)
    assert len(events) == 2
    field_names = [e.field_name for e in events]
    assert "custom_tax_bracket" in field_names
    assert "remote_stipend_eligible" in field_names
    assert events[0].severity == DriftSeverity.INFO


def test_schema_drift_handles_host_http_400_rejection():
    """Verify interceptor diagnoses missing required fields from host 400 Bad Request responses."""
    interceptor = SchemaDriftInterceptor()
    mapping = EntityMapping(
        vendor="test_hrms",
        entity_type="REQUISITION",
        id_path="id",
        fields=(FieldMap(target="title", source="title"),),
    )

    error_resp = {
        "status": 400,
        "error": "Bad Request",
        "message": "Validation failed: 'departmentId' cannot be null or is required.",
    }

    event = interceptor.inspect_error_payload(
        entity_type=EntityType.REQUISITION,
        mapping=mapping,
        outbound_payload={"title": "Software Engineer"},
        host_error_response=error_resp,
        status_code=400,
    )

    assert event.severity == DriftSeverity.CRITICAL
    assert "departmentId" in event.field_name
    assert "departmentId" in str(event.suggested_patch)

    # Test autonomous remediation delta generation
    delta = interceptor.generate_remediation_delta(event)
    assert delta["status"] == "APPLIED"
    assert delta["target_entity"] == "REQUISITION"
    assert delta["field_name"] == event.field_name
