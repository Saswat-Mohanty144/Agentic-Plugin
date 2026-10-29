"""Coverage tests for hrms_plugin/connectors/profiles/iceipts.py."""

from __future__ import annotations

import base64
import json

import pytest

from hrms_plugin.connectors.profiles.iceipts import IceiptsProfile, extract_jwt_user_id
from hrms_plugin.schema.canonical import EntityType


def _make_jwt(payload_dict: dict) -> str:
    """Helper to create an unverified 3-part JWT."""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(json.dumps(payload_dict).encode()).decode().rstrip("=")
    return f"{header}.{payload}.signature"


def test_extract_jwt_user_id_various_fields_and_formats():
    """Positive and boundary tests for extracting user ID from JWT payload claims."""
    # None or missing dot
    assert extract_jwt_user_id(None) is None
    assert extract_jwt_user_id("") is None
    assert extract_jwt_user_id("nodotsinthisstring") is None

    # Single dot only (parts < 2 check)
    assert extract_jwt_user_id("singlepart.") is None

    # Claim 'id'
    jwt_id = _make_jwt({"id": "usr-001"})
    assert extract_jwt_user_id(f"Bearer {jwt_id}") == "usr-001"

    # Claim 'cid'
    jwt_cid = _make_jwt({"cid": "cid-002"})
    assert extract_jwt_user_id(jwt_cid) == "cid-002"

    # Claim 'userId'
    jwt_uid = _make_jwt({"userId": "uid-003"})
    assert extract_jwt_user_id(jwt_uid) == "uid-003"

    # Claim 'user_id'
    jwt_user_id = _make_jwt({"user_id": "uid-004"})
    assert extract_jwt_user_id(jwt_user_id) == "uid-004"

    # Invalid base64 or corrupt json
    assert extract_jwt_user_id("invalid.!!!base64!!!.sig") is None


def test_iceipts_profile_endpoint_for_all_entities_and_actions():
    """Positive and negative tests for iCeipts routing across all canonical entities."""
    prof = IceiptsProfile(
        frontend_origin="http://localhost:4000",
        default_user_id="11111111-1111-1111-1111-111111111111",
    )

    # REQUISITION
    assert (
        prof.endpoint_for(EntityType.REQUISITION, "create", user_id="user-override")
        == "/job/createRequisition/user-override"
    )
    assert prof.endpoint_for(EntityType.REQUISITION, "get", entity_id="J-10") == "/job/detail/J-10"
    assert prof.endpoint_for(EntityType.REQUISITION, "list") == "/job/list"
    with pytest.raises(ValueError) as exc:
        prof.endpoint_for(EntityType.REQUISITION, "get", entity_id=None)
    assert "Fetching an iCeipts requisition requires entity_id" in str(exc.value)

    # EMPLOYEE
    assert prof.endpoint_for(EntityType.EMPLOYEE, "get", entity_id="E-1") == "/employee/profile/E-1"
    assert prof.endpoint_for(EntityType.EMPLOYEE, "list") == "/employee/list"
    assert prof.endpoint_for(EntityType.EMPLOYEE, "update", entity_id="E-1") == "/employee/E-1"
    with pytest.raises(ValueError) as exc_e_get:
        prof.endpoint_for(EntityType.EMPLOYEE, "get", entity_id=None)
    assert "Fetching an iCeipts employee requires entity_id" in str(exc_e_get.value)
    with pytest.raises(ValueError) as exc_e_up:
        prof.endpoint_for(EntityType.EMPLOYEE, "update", entity_id=None)
    assert "Updating an iCeipts employee requires entity_id" in str(exc_e_up.value)

    # LEAVE_REQUEST
    assert prof.endpoint_for(EntityType.LEAVE_REQUEST, "create") == "/leave/apply"
    assert prof.endpoint_for(EntityType.LEAVE_REQUEST, "get", entity_id="L-1") == "/leave/detail/L-1"
    assert prof.endpoint_for(EntityType.LEAVE_REQUEST, "list") == "/leave/list"
    assert prof.endpoint_for(EntityType.LEAVE_REQUEST, "update", entity_id="L-1") == "/leave/status/L-1"
    with pytest.raises(ValueError) as exc_l_get:
        prof.endpoint_for(EntityType.LEAVE_REQUEST, "get", entity_id=None)
    assert "Fetching an iCeipts leave request requires entity_id" in str(exc_l_get.value)
    with pytest.raises(ValueError) as exc_l_up:
        prof.endpoint_for(EntityType.LEAVE_REQUEST, "update", entity_id=None)
    assert "Updating an iCeipts leave status requires entity_id" in str(exc_l_up.value)

    # CANDIDATE
    assert prof.endpoint_for(EntityType.CANDIDATE, "get", entity_id="C-1") == "/candidate/detail/C-1"
    assert prof.endpoint_for(EntityType.CANDIDATE, "list") == "/candidate/list"
    with pytest.raises(ValueError) as exc_c_get:
        prof.endpoint_for(EntityType.CANDIDATE, "get", entity_id=None)
    assert "Fetching an iCeipts candidate requires entity_id" in str(exc_c_get.value)

    # PUNCH
    assert prof.endpoint_for(EntityType.PUNCH, "create") == "/attendance/punch"
    assert prof.endpoint_for(EntityType.PUNCH, "list") == "/attendance/history"

    # Fallback entity
    assert prof.endpoint_for(EntityType.ASSET, "get", entity_id="A-1") == "/assets/A-1"
    assert prof.endpoint_for(EntityType.ASSET, "list") == "/assets"


def test_iceipts_profile_prepare_headers_and_custom():
    """Positive test: prepare_headers format, Bearer prefixing, and custom headers."""
    prof = IceiptsProfile(frontend_origin="https://hrms.corp.internal")

    # Raw token gets Bearer prefix
    h1 = prof.prepare_headers(auth_token="token_val")
    assert h1["Authorization"] == "Bearer token_val"
    assert h1["Origin"] == "https://hrms.corp.internal"
    assert h1["Referer"] == "https://hrms.corp.internal/"

    # Token with existing Bearer
    h2 = prof.prepare_headers(auth_token="Bearer existing_val")
    assert h2["Authorization"] == "Bearer existing_val"

    # Custom headers
    h3 = prof.prepare_headers(custom_headers={"X-Test-Id": "t-100"})
    assert h3["X-Test-Id"] == "t-100"


def test_iceipts_profile_unwrap_response():
    """Positive and boundary tests for unwrapping iCeipts job, rows, items, and list payloads."""
    prof = IceiptsProfile()

    # Non-dict
    assert prof.unwrap_response("raw_string", "get", EntityType.REQUISITION) == "raw_string"

    # Requisition data.job wrapper
    r1 = prof.unwrap_response({"data": {"job": {"jobId": "j-1"}}}, "get", EntityType.REQUISITION)
    assert r1 == {"jobId": "j-1"}

    # data.rows wrapper
    r2 = prof.unwrap_response({"data": {"rows": [{"id": 1}]}}, "list", EntityType.EMPLOYEE)
    assert r2 == [{"id": 1}]

    # data.items wrapper
    r3 = prof.unwrap_response({"data": {"items": [{"id": 2}]}}, "list", EntityType.EMPLOYEE)
    assert r3 == [{"id": 2}]

    # data direct dictionary
    r4 = prof.unwrap_response({"data": {"id": 3, "val": "x"}}, "get", EntityType.EMPLOYEE)
    assert r4 == {"id": 3, "val": "x"}

    # data is a list
    r5 = prof.unwrap_response({"data": [{"id": 4}]}, "list", EntityType.EMPLOYEE)
    assert r5 == [{"id": 4}]

    # data is None / missing
    r6 = prof.unwrap_response({"status": "empty"}, "get", EntityType.EMPLOYEE)
    assert r6 == {"status": "empty"}


def test_iceipts_profile_normalize_payload():
    """Positive and boundary tests for iCeipts requisition normalization and UUID filtering."""
    prof = IceiptsProfile()

    # Non-requisition entity passes through unchanged
    p_emp = {"name": "Bob"}
    assert prof.normalize_payload_for_host(p_emp, EntityType.EMPLOYEE, "create") == p_emp

    # Requisition with invalid position string and valid department UUID
    dept_uuid = "a1b2c3d4-e5f6-7a8b-9c0d-1e2f3a4b5c6d"
    skill_uuid = "00000000-0000-0000-0000-000000000001"
    benefit_uuid = "00000000-0000-0000-0000-000000000002"

    req_payload = {
        "title": "Principal Engineer",
        "department": "Platform",
        "department_id": dept_uuid,
        "position": "invalid_number_count",
        "skills": [skill_uuid, "non_uuid_skill_tag"],
        "benefits": [benefit_uuid, "free_coffee"],
        "min_salary": 20,
        "max_salary": 35,
        "work_model": "remote",
        "employment_type": "full_time",
    }

    norm = prof.normalize_payload_for_host(req_payload, EntityType.REQUISITION, "create")
    assert norm["jobData"]["departmentId"] == dept_uuid
    assert norm["jobData"]["position"] == 1  # fell back to 1
    assert norm["jobData"]["salary"] == "20-35 LPA"
    assert norm["jobData"]["jobMode"] == "WorkFromHome"
    assert norm["jobData"]["type"] == "Full-time"
    assert norm["jobData"]["recuiterId"] == prof.default_user_id
    assert norm["skills"] == [skill_uuid]
    assert norm["benefits"] == [benefit_uuid]
