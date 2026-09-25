"""Tests for extended vendor profiles: Frappe, BambooHR, and Workday."""

from __future__ import annotations

from hrms_plugin.connectors.profiles import (
    BambooHRProfile,
    FrappeProfile,
    WorkdayProfile,
    get_vendor_profile,
)
from hrms_plugin.schema.canonical import EntityType


def test_frappe_profile_routing_and_headers():
    """Verify Frappe DocType routing and token authentication headers."""
    profile = get_vendor_profile("frappe", company="Acme Corp")
    assert isinstance(profile, FrappeProfile)

    # 1. Routing
    emp_ep = profile.endpoint_for(EntityType.EMPLOYEE, "list")
    assert emp_ep == "/api/resource/Employee"

    emp_detail = profile.endpoint_for(EntityType.EMPLOYEE, "get", entity_id="EMP-001")
    assert emp_detail == "/api/resource/Employee/EMP-001"

    req_ep = profile.endpoint_for(EntityType.REQUISITION, "create")
    assert req_ep == "/api/resource/Job Requisition"

    leave_ep = profile.endpoint_for(EntityType.LEAVE_REQUEST, "apply")
    assert leave_ep == "/api/resource/Leave Application"

    # 2. Headers
    headers = profile.prepare_headers(auth_token="api_key:secret_key")
    assert headers["Authorization"] == "token api_key:secret_key"
    assert "application/json" in headers["Content-Type"]

    # 3. Payload normalization
    norm = profile.normalize_payload_for_host(
        canonical_payload={"full_name": "Aarav Sharma", "title": "Staff Engineer"},
        entity_type=EntityType.EMPLOYEE,
        action="create",
    )
    assert norm["first_name"] == "Aarav"
    assert norm["last_name"] == "Sharma"
    assert norm["company"] == "Acme Corp"


def test_bamboohr_profile_routing_and_auth():
    """Verify BambooHR API v1 routing and Basic Auth encoding."""
    profile = get_vendor_profile("bamboohr")
    assert isinstance(profile, BambooHRProfile)

    # 1. Directory and Employee routing
    dir_ep = profile.endpoint_for(EntityType.EMPLOYEE, "directory")
    assert dir_ep == "/v1/employees/directory"

    emp_ep = profile.endpoint_for(EntityType.EMPLOYEE, "get", entity_id="123")
    assert emp_ep == "/v1/employees/123"

    # 2. Leave and Requisition
    leave_ep = profile.endpoint_for(EntityType.LEAVE_REQUEST, "list")
    assert leave_ep == "/v1/time_off/requests"

    # 3. Basic Auth header
    headers = profile.prepare_headers(auth_token="test_api_key_123")
    assert "Basic " in headers["Authorization"]

    # 4. Unwrapping
    unwrapped = profile.unwrap_response(
        {"employees": [{"id": "1", "name": "John Doe"}]},
        action="list",
        entity_type=EntityType.EMPLOYEE,
    )
    assert isinstance(unwrapped, list)
    assert unwrapped[0]["name"] == "John Doe"


def test_workday_profile_routing_and_tenant():
    """Verify Workday tenant routing and bearer authentication."""
    profile = get_vendor_profile("workday", tenant_name="enterprise_global")
    assert isinstance(profile, WorkdayProfile)

    # 1. Routing
    workers_ep = profile.endpoint_for(EntityType.EMPLOYEE, "list")
    assert workers_ep == "/ccx/api/v1/enterprise_global/workers"

    req_ep = profile.endpoint_for(EntityType.REQUISITION, "get", entity_id="REQ-99")
    assert req_ep == "/ccx/api/v1/enterprise_global/jobRequisitions/REQ-99"

    # 2. Bearer Token Auth
    headers = profile.prepare_headers(auth_token="oauth_token_xyz")
    assert headers["Authorization"] == "Bearer oauth_token_xyz"
