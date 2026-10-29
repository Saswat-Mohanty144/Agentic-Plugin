"""Coverage tests for hrms_plugin/connectors/profiles/zoho.py."""

from __future__ import annotations

from hrms_plugin.connectors.profiles.zoho import ZohoProfile
from hrms_plugin.schema.canonical import EntityType


def test_zoho_profile_endpoint_routing_all_actions_and_entities():
    """Positive and boundary tests for Zoho Form API endpoint construction."""
    prof = ZohoProfile(default_department="Engineering")

    # Punch actions
    assert prof.endpoint_for(EntityType.PUNCH, "create") == "/attendance/punchIn"
    assert prof.endpoint_for(EntityType.PUNCH, "punch_in") == "/attendance/punchIn"
    assert prof.endpoint_for(EntityType.PUNCH, "punch_out") == "/attendance/punchOut"
    assert prof.endpoint_for(EntityType.PUNCH, "list") == "/attendance/getUserAttendance"

    # Leave balance
    assert prof.endpoint_for(EntityType.LEAVE_BALANCE, "list") == "/forms/json/leave/getEmployeeLeaveTypes"

    # Standard forms: get with and without entity_id
    assert (
        prof.endpoint_for(EntityType.EMPLOYEE, "get", entity_id="Z100")
        == "/forms/json/employee/getRecordByID?recordId=Z100"
    )
    assert prof.endpoint_for(EntityType.EMPLOYEE, "get", entity_id=None) == "/forms/json/employee/getRecords"
    assert prof.endpoint_for(EntityType.EMPLOYEE, "read") == "/forms/json/employee/getRecords"

    # Update with and without entity_id
    assert (
        prof.endpoint_for(EntityType.EMPLOYEE, "update", entity_id="Z100")
        == "/forms/json/employee/updateRecord?recordId=Z100"
    )
    assert prof.endpoint_for(EntityType.EMPLOYEE, "update", entity_id=None) == "/forms/json/employee/updateRecord"

    # Delete with and without entity_id
    assert (
        prof.endpoint_for(EntityType.EMPLOYEE, "delete", entity_id="Z100")
        == "/forms/json/employee/deleteRecord?recordId=Z100"
    )
    assert prof.endpoint_for(EntityType.EMPLOYEE, "delete", entity_id=None) == "/forms/json/employee/deleteRecord"

    # Fallback action
    assert prof.endpoint_for(EntityType.EMPLOYEE, "custom_action") == "/forms/json/employee/getRecords"


def test_zoho_profile_prepare_headers_token_formats():
    """Positive test: Zoho auth token header prefixing and custom header merging."""
    prof = ZohoProfile(default_headers={"X-Zoho-Domain": "zoho.com"})

    # Raw token gets Zoho-oauthtoken prefix
    h1 = prof.prepare_headers(auth_token="1000.abc123xyz")
    assert h1["Authorization"] == "Zoho-oauthtoken 1000.abc123xyz"
    assert h1["X-Zoho-Domain"] == "zoho.com"

    # Pre-formatted Zoho-oauthtoken
    h2 = prof.prepare_headers(auth_token="Zoho-oauthtoken existing_token")
    assert h2["Authorization"] == "Zoho-oauthtoken existing_token"

    # Pre-formatted Bearer
    h3 = prof.prepare_headers(auth_token="Bearer bearer_tok")
    assert h3["Authorization"] == "Bearer bearer_tok"

    # Custom headers
    h4 = prof.prepare_headers(custom_headers={"X-App-Env": "sandbox"})
    assert h4["X-App-Env"] == "sandbox"


def test_zoho_profile_unwrap_response():
    """Positive and boundary tests for unwrapping Zoho nested envelopes."""
    prof = ZohoProfile()

    # Non-dictionary
    assert prof.unwrap_response("raw-string", "get", EntityType.EMPLOYEE) == "raw-string"

    # Single-key dictionary inside single-item list
    single_wrap = {"response": {"result": [{"EmployeeDetails": {"Name": "Bob", "ID": "101"}}]}}
    assert prof.unwrap_response(single_wrap, "get", EntityType.EMPLOYEE) == {"Name": "Bob", "ID": "101"}

    # Multi-item list result
    multi_res = {"response": {"result": [{"id": 1}, {"id": 2}]}}
    assert prof.unwrap_response(multi_res, "list", EntityType.EMPLOYEE) == [{"id": 1}, {"id": 2}]

    # response.data envelope
    data_wrap = {"response": {"data": [{"id": 3}]}}
    assert prof.unwrap_response(data_wrap, "list", EntityType.EMPLOYEE) == [{"id": 3}]

    # Root data envelope
    root_data = {"data": [{"id": 4}]}
    assert prof.unwrap_response(root_data, "list", EntityType.EMPLOYEE) == [{"id": 4}]

    # Fallback root dictionary
    other = {"status": "ok"}
    assert prof.unwrap_response(other, "get", EntityType.EMPLOYEE) == {"status": "ok"}


def test_zoho_profile_normalize_payload_all_entities():
    """Positive and boundary tests for normalizing canonical entities into Zoho form fields."""
    prof = ZohoProfile(default_department="Engineering")

    # Employee with single word name
    p_single = prof.normalize_payload_for_host(
        {"full_name": "Cher", "work_email": "cher@example.com"},
        EntityType.EMPLOYEE,
        "create",
    )
    assert p_single["FirstName"] == "Cher"
    assert p_single["LastName"] == "Cher"
    assert p_single["EmailID"] == "cher@example.com"
    assert p_single["Department"] == "Engineering"

    # Employee with explicit first_name, last_name, phone, designation, joining_date
    p_full = prof.normalize_payload_for_host(
        {
            "first_name": "Alice",
            "last_name": "Smith",
            "phone": "+971500000000",
            "department": "Product",
            "designation": "Principal",
            "joining_date": "2026-01-15",
        },
        EntityType.EMPLOYEE,
        "create",
    )
    assert p_full["FirstName"] == "Alice"
    assert p_full["LastName"] == "Smith"
    assert p_full["Mobile"] == "+971500000000"
    assert p_full["Department"] == "Product"
    assert p_full["Designation"] == "Principal"
    assert p_full["Dateofjoining"] == "2026-01-15"

    # Leave request normalization
    p_leave = prof.normalize_payload_for_host(
        {
            "leave_type": "sick leave",
            "start_date": "2026-04-01",
            "end_date": "2026-04-03",
            "reason": "Flu symptoms",
            "employee_ref": "EMP-900",
        },
        EntityType.LEAVE_REQUEST,
        "create",
    )
    assert p_leave["LeaveType"] == "Sick Leave"
    assert p_leave["From"] == "2026-04-01"
    assert p_leave["To"] == "2026-04-03"
    assert p_leave["Reasonforleave"] == "Flu symptoms"
    assert p_leave["Employee_ID"] == "EMP-900"

    # Requisition normalization
    p_req = prof.normalize_payload_for_host(
        {
            "title": "Backend Architect",
            "department": "Core Platform",
            "description": "Design distributed microservices",
        },
        EntityType.REQUISITION,
        "create",
    )
    assert p_req["JobTitle"] == "Backend Architect"
    assert p_req["Department"] == "Core Platform"
    assert p_req["JobDescription"] == "Design distributed microservices"

    # Punch normalization
    p_punch = prof.normalize_payload_for_host(
        {"employee_ref": "E-501", "punch_time": "2026-10-29T08:00:00Z"},
        EntityType.PUNCH,
        "create",
    )
    assert p_punch["empId"] == "E-501"
    assert p_punch["punchTime"] == "2026-10-29T08:00:00Z"


def test_zoho_profile_extract_error_message():
    """Positive and boundary tests for extracting error messages from Zoho responses."""
    prof = ZohoProfile()

    # response.errors.message
    b1 = {"response": {"errors": {"message": "Invalid date format for From"}}}
    assert prof.extract_error_message(b1, "Default") == "Invalid date format for From"

    # response.errors string
    b2 = {"response": {"errors": "Missing required field: Department"}}
    assert prof.extract_error_message(b2, "Default") == "Missing required field: Department"

    # response.message
    b3 = {"response": {"message": "Permission denied"}}
    assert prof.extract_error_message(b3, "Default") == "Permission denied"

    # top-level message
    b4 = {"message": "Rate limit exceeded"}
    assert prof.extract_error_message(b4, "Default") == "Rate limit exceeded"

    # fallback
    assert prof.extract_error_message({}, "Fallback Error") == "Fallback Error"
    assert prof.extract_error_message("raw text", "Fallback Error") == "Fallback Error"


def test_zoho_profile_heal_payload_from_error():
    """Positive and negative tests for self-healing missing department and lastname."""
    prof = ZohoProfile(default_department="Operations")

    # Heal missing Department
    p1 = {"FirstName": "Bob"}
    healed1 = prof.heal_payload_from_error(p1, EntityType.EMPLOYEE, "Error: department is required")
    assert healed1 is not None
    assert healed1["Department"] == "Operations"

    # Heal missing LastName when FirstName exists
    p2 = {"FirstName": "Alice"}
    healed2 = prof.heal_payload_from_error(p2, EntityType.EMPLOYEE, "Field lastname cannot be empty")
    assert healed2 is not None
    assert healed2["LastName"] == "Alice"

    # Error without match returns None
    assert prof.heal_payload_from_error(p1, EntityType.EMPLOYEE, "Connection timed out") is None
