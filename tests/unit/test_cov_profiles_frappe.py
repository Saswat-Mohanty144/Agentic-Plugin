"""Coverage tests for hrms_plugin/connectors/profiles/frappe.py."""

from __future__ import annotations

from hrms_plugin.connectors.profiles.frappe import FrappeProfile
from hrms_plugin.schema.canonical import EntityType


def test_frappe_profile_endpoint_for_all_doctypes():
    """Positive test: Frappe endpoints correctly map canonical entity types to DocTypes."""
    prof = FrappeProfile(company="Acme Corp")

    # Document-level endpoints
    req_ep = "/api/resource/Job Requisition/REQ-101"
    leave_ep = "/api/resource/Leave Application/LV-101"
    cand_ep = "/api/resource/Job Applicant/APP-101"
    punch_ep = "/api/resource/Employee Checkin/CHK-101"

    assert prof.endpoint_for(EntityType.EMPLOYEE, "get", entity_id="EMP-101") == "/api/resource/Employee/EMP-101"
    assert prof.endpoint_for(EntityType.REQUISITION, "update", entity_id="REQ-101") == req_ep
    assert prof.endpoint_for(EntityType.LEAVE_REQUEST, "fetch", entity_id="LV-101") == leave_ep
    assert prof.endpoint_for(EntityType.CANDIDATE, "patch", entity_id="APP-101") == cand_ep
    assert prof.endpoint_for(EntityType.PUNCH, "delete", entity_id="CHK-101") == punch_ep

    # Collection-level endpoints
    assert prof.endpoint_for(EntityType.EMPLOYEE, "list") == "/api/resource/Employee"
    assert prof.endpoint_for(EntityType.EMPLOYEE, "get", entity_id=None) == "/api/resource/Employee"

    # Fallback entity type
    assert prof.endpoint_for(EntityType.ASSET, "list") == "/api/resource/Asset"


def test_frappe_profile_prepare_headers_token_formats():
    """Positive and boundary tests for Frappe token header prefix handling."""
    prof = FrappeProfile(company="Acme Corp", default_headers={"X-Frappe-Site": "acme.erpnext.com"})

    # Raw key without prefix gets 'token ' prefix
    h1 = prof.prepare_headers(auth_token="api_key:api_secret")
    assert h1["Authorization"] == "token api_key:api_secret"
    assert h1["X-Frappe-Site"] == "acme.erpnext.com"

    # Existing 'token ' prefix preserved
    h2 = prof.prepare_headers(auth_token="token existing_key:existing_secret")
    assert h2["Authorization"] == "token existing_key:existing_secret"

    # Existing 'Bearer ' prefix preserved
    h3 = prof.prepare_headers(auth_token="Bearer bearer_token_xyz")
    assert h3["Authorization"] == "Bearer bearer_token_xyz"

    # Custom headers merged
    h4 = prof.prepare_headers(auth_token="tok", custom_headers={"X-Custom": "val123"})
    assert h4["X-Custom"] == "val123"
    assert h4["Authorization"] == "token tok"


def test_frappe_profile_unwrap_response():
    """Positive and boundary tests for Frappe message and data response unwrapping."""
    prof = FrappeProfile()

    # Data envelope
    assert prof.unwrap_response({"data": {"name": "EMP-001"}}, "get", EntityType.EMPLOYEE) == {"name": "EMP-001"}

    # Message envelope with dict
    msg_dict = {"message": {"status": "success"}}
    assert prof.unwrap_response(msg_dict, "post", EntityType.EMPLOYEE) == {"status": "success"}

    # Message envelope with list
    assert prof.unwrap_response({"message": ["doc1", "doc2"]}, "list", EntityType.EMPLOYEE) == ["doc1", "doc2"]

    # Message with string (non dict/list) falls through to root dict
    msg_str = {"message": "Operation completed"}
    assert prof.unwrap_response(msg_str, "post", EntityType.EMPLOYEE) == msg_str

    # Non-dictionary payload
    assert prof.unwrap_response("raw string response", "get", EntityType.EMPLOYEE) == "raw string response"


def test_frappe_profile_normalize_payload_employee_and_splits():
    """Positive and boundary tests for employee name splitting and company defaults."""
    prof = FrappeProfile(company="Test Global Corp")

    # Full name with two words
    res1 = prof.normalize_payload_for_host(
        {"full_name": "Jane Doe", "department": "Operations"},
        EntityType.EMPLOYEE,
        "create",
    )
    assert res1["first_name"] == "Jane"
    assert res1["last_name"] == "Doe"
    assert res1["company"] == "Test Global Corp"

    # Full name with single word
    res2 = prof.normalize_payload_for_host(
        {"full_name": "Cher"},
        EntityType.EMPLOYEE,
        "create",
    )
    assert res2["first_name"] == "Cher"
    assert "last_name" not in res2
    assert res2["company"] == "Test Global Corp"


def test_frappe_profile_normalize_payload_leave_and_requisition():
    """Positive test: Normalize LEAVE_REQUEST and REQUISITION fields."""
    prof = FrappeProfile(company="Enterprise Ltd")

    # Leave request normalization
    res_leave = prof.normalize_payload_for_host(
        {"leave_type": "annual leave", "from_date": "2026-05-01", "to_date": "2026-05-10"},
        EntityType.LEAVE_REQUEST,
        "create",
    )
    assert res_leave["leave_type"] == "Annual Leave"
    assert res_leave["from_date"] == "2026-05-01"
    assert res_leave["to_date"] == "2026-05-10"

    # Requisition normalization (title -> designation, company)
    res_req = prof.normalize_payload_for_host(
        {"title": "Senior Staff Architect"},
        EntityType.REQUISITION,
        "create",
    )
    assert res_req["designation"] == "Senior Staff Architect"
    assert res_req["company"] == "Enterprise Ltd"


def test_frappe_profile_heal_payload_from_error():
    """Positive and negative tests for self-healing missing company and naming_series."""
    prof = FrappeProfile(company="Self Heal Corp")

    # Heal missing company
    p1 = {"first_name": "Bob"}
    healed1 = prof.heal_payload_from_error(p1, EntityType.EMPLOYEE, "Mandatory Error: Missing company")
    assert healed1 is not None
    assert healed1["company"] == "Self Heal Corp"
    assert healed1["first_name"] == "Bob"

    # Heal missing naming_series for employee
    p2 = {"first_name": "Bob", "company": "Self Heal Corp"}
    healed2 = prof.heal_payload_from_error(p2, EntityType.EMPLOYEE, "Naming_series is mandatory")
    assert healed2 is not None
    assert healed2["naming_series"] == "EMP-"

    # Heal missing naming_series for requisition
    p3 = {"designation": "Manager", "company": "Self Heal Corp"}
    healed3 = prof.heal_payload_from_error(p3, EntityType.REQUISITION, "Naming_series is mandatory")
    assert healed3 is not None
    assert healed3["naming_series"] == "HR-"

    # Unrecognized error returns None
    assert prof.heal_payload_from_error(p1, EntityType.EMPLOYEE, "Database timeout") is None
