"""Coverage tests for hrms_plugin/connectors/profiles/workday.py."""

from __future__ import annotations

from hrms_plugin.connectors.profiles.workday import WorkdayProfile
from hrms_plugin.schema.canonical import EntityType


def test_workday_endpoint_for_all_entities_and_tenant_override():
    """Positive and boundary tests for Workday routing and tenant override."""
    prof = WorkdayProfile(tenant_name="acme_prod", api_prefix="/ccx/api/v1/")

    # EMPLOYEE
    assert prof.endpoint_for(EntityType.EMPLOYEE, "get", entity_id="W100") == "/ccx/api/v1/acme_prod/workers/W100"
    assert prof.endpoint_for(EntityType.EMPLOYEE, "fetch", entity_id="W100") == "/ccx/api/v1/acme_prod/workers/W100"
    assert prof.endpoint_for(EntityType.EMPLOYEE, "update", entity_id="W100") == "/ccx/api/v1/acme_prod/workers/W100"
    assert prof.endpoint_for(EntityType.EMPLOYEE, "list") == "/ccx/api/v1/acme_prod/workers"

    # Tenant override in kwargs
    assert (
        prof.endpoint_for(EntityType.EMPLOYEE, "list", tenant="tenant_override")
        == "/ccx/api/v1/tenant_override/workers"
    )

    # LEAVE_REQUEST
    lr_ep = "/ccx/api/v1/acme_prod/timeOffRequests/LR1"
    assert prof.endpoint_for(EntityType.LEAVE_REQUEST, "get", entity_id="LR1") == lr_ep
    assert prof.endpoint_for(EntityType.LEAVE_REQUEST, "fetch", entity_id="LR1") == lr_ep
    assert prof.endpoint_for(EntityType.LEAVE_REQUEST, "list") == "/ccx/api/v1/acme_prod/timeOffRequests"

    # REQUISITION
    req_ep = "/ccx/api/v1/acme_prod/jobRequisitions/JR1"
    assert prof.endpoint_for(EntityType.REQUISITION, "get", entity_id="JR1") == req_ep
    assert prof.endpoint_for(EntityType.REQUISITION, "fetch", entity_id="JR1") == req_ep
    assert prof.endpoint_for(EntityType.REQUISITION, "create") == "/ccx/api/v1/acme_prod/jobRequisitions"

    # CANDIDATE
    cand_ep = "/ccx/api/v1/acme_prod/jobApplications/JA1"
    assert prof.endpoint_for(EntityType.CANDIDATE, "get", entity_id="JA1") == cand_ep
    assert prof.endpoint_for(EntityType.CANDIDATE, "fetch", entity_id="JA1") == cand_ep
    assert prof.endpoint_for(EntityType.CANDIDATE, "list") == "/ccx/api/v1/acme_prod/jobApplications"

    # PUNCH
    assert prof.endpoint_for(EntityType.PUNCH, "post") == "/ccx/api/v1/acme_prod/timeClockEvents"

    # Fallback entity
    assert prof.endpoint_for(EntityType.ASSET, "get", entity_id="A99") == "/ccx/api/v1/acme_prod/assets/A99"
    assert prof.endpoint_for(EntityType.ASSET, "list") == "/ccx/api/v1/acme_prod/assets"


def test_workday_prepare_headers_auth_and_custom():
    """Positive test: prepare_headers format and custom header merging."""
    prof = WorkdayProfile()

    # Raw token
    h_raw = prof.prepare_headers(auth_token="jwt_token_123")
    assert h_raw["Authorization"] == "Bearer jwt_token_123"
    assert h_raw["User-Agent"] == "hrms-agentic-plugin/1.0 (+workday-profile)"

    # Pre-formatted Bearer token
    h_bearer = prof.prepare_headers(auth_token="Bearer existing_jwt_token")
    assert h_bearer["Authorization"] == "Bearer existing_jwt_token"

    # Custom headers
    h_custom = prof.prepare_headers(custom_headers={"X-Workday-Client": "v2"})
    assert h_custom["X-Workday-Client"] == "v2"


def test_workday_unwrap_response():
    """Positive and boundary tests for Workday response payloads and RaaS envelopes."""
    prof = WorkdayProfile()

    # data envelope
    assert prof.unwrap_response({"data": [{"id": "w1"}]}, "list", EntityType.EMPLOYEE) == [{"id": "w1"}]

    # Report_Entry envelope (RaaS)
    assert prof.unwrap_response({"Report_Entry": [{"worker": "1"}]}, "list", EntityType.EMPLOYEE) == [{"worker": "1"}]

    # items envelope
    assert prof.unwrap_response({"items": [1, 2]}, "list", EntityType.EMPLOYEE) == [1, 2]

    # Non-enveloped dict
    assert prof.unwrap_response({"status": "active"}, "get", EntityType.EMPLOYEE) == {"status": "active"}

    # Non-dict response
    assert prof.unwrap_response(12345, "get", EntityType.EMPLOYEE) == 12345


def test_workday_normalize_payload():
    """Positive test: normalize_payload_for_host preserves payload dictionary."""
    prof = WorkdayProfile()
    payload = {"worker_id": "W123", "status": "active"}
    assert prof.normalize_payload_for_host(payload, EntityType.EMPLOYEE, "update") == payload
