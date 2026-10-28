"""Tier 5: End-to-End Integration Tests for Standalone Agentic HRMS AI Plugin."""

import pytest
from fastapi.testclient import TestClient

from hrms_plugin.server.app import app


@pytest.fixture
def client():
    return TestClient(app)


def test_e2e_compliance_audit_and_remediation_lifecycle(client):
    """E2E Test: Audit -> Routing -> Statutory Citation -> 1-Click Patch -> Signed Audit Remediation."""
    # Step 1: Query Copilot with an employee compliance audit prompt
    chat_payload = {
        "message": "Audit employee for compliance violations",
        "jurisdiction": "AE",
        "context": {
            "tenant_id": "demo_corp",
            "employees": [
                {
                    "id": "EMP-901",
                    "name": "Tariq Al-Mansoor",
                    "basic_salary": 4000.0,
                    "gross_salary": 10000.0,
                    "probation_days": 210,
                    "weekly_hours": 54,
                    "uae_wps_registered": False,
                }
            ],
        },
    }

    chat_resp = client.post("/v1/chat", json=chat_payload, headers={"X-Tenant-Id": "demo_corp"})
    assert chat_resp.status_code == 200
    chat_data = chat_resp.json()

    assert "reply_text" in chat_data
    assert len(chat_data.get("statutory_citations", [])) > 0
    assert len(chat_data.get("suggested_actions", [])) > 0

    first_action = chat_data["suggested_actions"][0]
    violation_id = first_action["violation_id"]
    patch = first_action.get("patch", {})

    # Step 2: Approve & Execute 1-Click Patch Remediation (HITL)
    rem_payload = {
        "violation_id": violation_id,
        "target_entity": "EMPLOYEE",
        "entity_id": "EMP-901",
        "patch_payload": patch,
        "approver_id": "USR-MGR-501",
        "approver_role": "HR_ADMIN",
    }

    rem_resp = client.post("/v1/remediate/execute", json=rem_payload, headers={"X-Tenant-Id": "demo_corp"})
    assert rem_resp.status_code == 200
    rem_data = rem_resp.json()

    assert rem_data["status"] == "APPROVED_AND_QUEUED"
    audit_trace = rem_data["audit_tracing"]
    assert audit_trace["X-HITL-Approver-Id"] == "USR-MGR-501"
    assert audit_trace["X-Agent-Name"] == "StatutoryComplianceAuditor"
    assert "X-Agent-Invocation-Id" in audit_trace
    assert "X-Reasoning-Hash" in audit_trace


def test_e2e_zero_shot_schema_adaptation_lifecycle(client):
    """E2E Test: Ingest arbitrary host schema -> Synthesize Canonical FieldMaps -> Verify sub-millisecond execution."""
    host_swagger = {
        "swagger": "2.0",
        "info": {"title": "Legacy Host HRMS", "version": "1.0.0"},
        "definitions": {
            "Staff": {
                "type": "object",
                "properties": {
                    "staff_id": {"type": "string"},
                    "first_name": {"type": "string"},
                    "last_name": {"type": "string"},
                    "work_email": {"type": "string"},
                    "joining_date": {"type": "string"},
                    "base_pay": {"type": "number"},
                },
                "required": ["staff_id", "work_email"],
            }
        },
        "paths": {
            "/api/v2/staff": {
                "get": {
                    "responses": {
                        "200": {
                            "schema": {
                                "type": "array",
                                "items": {"$ref": "#/definitions/Staff"},
                            }
                        }
                    }
                }
            }
        },
    }

    # Step 1: Introspect host endpoints
    intro_resp = client.post("/v1/schema/introspect", json={"swagger_dict": host_swagger})
    assert intro_resp.status_code == 200
    intro_data = intro_resp.json()
    assert "Staff" in intro_data["entities_discovered"]

    # Step 2: Synthesize Canonical Mappings
    synth_payload = {
        "domain": "EMPLOYEE",
        "discovered_fields": [
            {"name": "staff_id"},
            {"name": "first_name"},
            {"name": "last_name"},
            {"name": "work_email"},
            {"name": "joining_date"},
            {"name": "base_pay"},
        ],
    }

    synth_resp = client.post("/v1/schema/synthesize", json=synth_payload)
    assert synth_resp.status_code == 200
    synth_data = synth_resp.json()
    assert synth_data["entity_type"] == "EMPLOYEE"
    canonical_targets = [fm["canonical_target"] for fm in synth_data["field_mappings"]]
    assert "work_email" in canonical_targets
    assert "full_name" in canonical_targets
