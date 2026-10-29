"""Coverage tests for hrms_plugin/connectors/profiles/bamboohr.py."""

from __future__ import annotations

import base64

from hrms_plugin.connectors.profiles.bamboohr import BambooHRProfile
from hrms_plugin.schema.canonical import EntityType


def test_bamboohr_endpoint_for_all_entities():
    """Positive and boundary tests for BambooHR routing across all supported entity types."""
    prof = BambooHRProfile(company_domain="mycompany", api_version="v1")

    # EMPLOYEE
    assert prof.endpoint_for(EntityType.EMPLOYEE, "directory") == "/v1/employees/directory"
    assert prof.endpoint_for(EntityType.EMPLOYEE, "list") == "/v1/employees/directory"
    assert prof.endpoint_for(EntityType.EMPLOYEE, "get", entity_id="123") == "/v1/employees/123"
    assert prof.endpoint_for(EntityType.EMPLOYEE, "create") == "/v1/employees"

    # LEAVE_REQUEST
    assert prof.endpoint_for(EntityType.LEAVE_REQUEST, "get", entity_id="L99") == "/v1/time_off/requests/L99"
    assert prof.endpoint_for(EntityType.LEAVE_REQUEST, "fetch", entity_id="L99") == "/v1/time_off/requests/L99"
    assert prof.endpoint_for(EntityType.LEAVE_REQUEST, "list") == "/v1/time_off/requests"

    # REQUISITION
    assert prof.endpoint_for(EntityType.REQUISITION, "get", entity_id="J10") == "/v1/applicant_tracking/jobs/J10"
    assert prof.endpoint_for(EntityType.REQUISITION, "fetch", entity_id="J10") == "/v1/applicant_tracking/jobs/J10"
    assert prof.endpoint_for(EntityType.REQUISITION, "list") == "/v1/applicant_tracking/jobs"

    # CANDIDATE
    cand_app = "/v1/applicant_tracking/applications/C55"
    assert prof.endpoint_for(EntityType.CANDIDATE, "get", entity_id="C55") == cand_app
    assert prof.endpoint_for(EntityType.CANDIDATE, "fetch", entity_id="C55") == cand_app
    assert prof.endpoint_for(EntityType.CANDIDATE, "list") == "/v1/applicant_tracking/applications"

    # PUNCH
    assert prof.endpoint_for(EntityType.PUNCH, "post") == "/v1/time_tracking/clock"

    # Fallback entity
    assert prof.endpoint_for(EntityType.ASSET, "get", entity_id="A1") == "/v1/assets/A1"
    assert prof.endpoint_for(EntityType.ASSET, "list") == "/v1/assets"


def test_bamboohr_prepare_headers_auth_encoding_and_custom():
    """Positive test: BambooHR API key encoding with dummy 'x' password and custom headers."""
    prof = BambooHRProfile()

    # Raw api key becomes Basic base64(key:x)
    expected_b64 = base64.b64encode(b"my_api_key_123:x").decode()
    h_raw = prof.prepare_headers(auth_token="my_api_key_123")
    assert h_raw["Authorization"] == f"Basic {expected_b64}"
    assert h_raw["User-Agent"] == "hrms-agentic-plugin/1.0 (+bamboohr-profile)"

    # Pre-formatted Basic header
    h_basic = prof.prepare_headers(auth_token="Basic already_encoded==")
    assert h_basic["Authorization"] == "Basic already_encoded=="

    # Pre-formatted Bearer header
    h_bearer = prof.prepare_headers(auth_token="Bearer bearer_tok")
    assert h_bearer["Authorization"] == "Bearer bearer_tok"

    # Custom headers
    h_custom = prof.prepare_headers(custom_headers={"X-Test": "val"})
    assert h_custom["X-Test"] == "val"


def test_bamboohr_unwrap_response_envelopes():
    """Positive and boundary tests for unwrapping BambooHR collection envelopes."""
    prof = BambooHRProfile()

    # employees envelope
    assert prof.unwrap_response({"employees": [{"id": "1"}]}, "list", EntityType.EMPLOYEE) == [{"id": "1"}]

    # requests envelope
    assert prof.unwrap_response({"requests": [{"id": "req-1"}]}, "list", EntityType.LEAVE_REQUEST) == [{"id": "req-1"}]

    # jobs envelope
    assert prof.unwrap_response({"jobs": [{"id": "job-1"}]}, "list", EntityType.REQUISITION) == [{"id": "job-1"}]

    # applications envelope
    assert prof.unwrap_response({"applications": [{"id": "app-1"}]}, "list", EntityType.CANDIDATE) == [{"id": "app-1"}]

    # Non-enveloped dict
    assert prof.unwrap_response({"other": "field"}, "get", EntityType.EMPLOYEE) == {"other": "field"}

    # Non-dict response
    assert prof.unwrap_response("raw_string", "get", EntityType.EMPLOYEE) == "raw_string"


def test_bamboohr_normalize_payload_employee_and_leave():
    """Positive and boundary tests for BambooHR payload field mapping."""
    prof = BambooHRProfile()

    # Employee with two-word full name
    p_emp2 = prof.normalize_payload_for_host(
        {"full_name": "Alice Smith", "department": "Tech"},
        EntityType.EMPLOYEE,
        "create",
    )
    assert p_emp2["firstName"] == "Alice"
    assert p_emp2["lastName"] == "Smith"

    # Employee with single-word full name
    p_emp1 = prof.normalize_payload_for_host(
        {"full_name": "Madonna"},
        EntityType.EMPLOYEE,
        "create",
    )
    assert p_emp1["firstName"] == "Madonna"
    assert p_emp1["lastName"] == ""

    # Leave request start_date/end_date mapping
    p_leave = prof.normalize_payload_for_host(
        {"start_date": "2026-06-01", "end_date": "2026-06-05"},
        EntityType.LEAVE_REQUEST,
        "create",
    )
    assert p_leave["start"] == "2026-06-01"
    assert p_leave["end"] == "2026-06-05"

    # Other entity types pass through
    p_other = prof.normalize_payload_for_host({"serial": "SN123"}, EntityType.ASSET, "create")
    assert p_other == {"serial": "SN123"}
