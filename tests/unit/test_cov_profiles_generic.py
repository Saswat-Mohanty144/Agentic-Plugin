"""Coverage tests for hrms_plugin/connectors/profiles/generic.py."""

from __future__ import annotations

import pytest

from hrms_plugin.connectors.profiles.generic import GenericProfile
from hrms_plugin.schema.canonical import EntityType


def test_generic_profile_custom_plural_overrides():
    """Positive test: Custom plural overrides update default entity plurals."""
    prof = GenericProfile(plural_overrides={EntityType.EMPLOYEE: "workers"})
    assert prof.plurals[EntityType.EMPLOYEE] == "workers"
    # Defaults preserved for other entities
    assert prof.plurals[EntityType.LEAVE_REQUEST] == "leave-requests"


def test_generic_profile_endpoint_for_actions_requiring_id():
    """Positive and negative tests for actions requiring entity_id."""
    prof = GenericProfile()

    # Success cases for actions requiring ID
    for act in ("get", "fetch", "update", "patch", "delete"):
        assert prof.endpoint_for(EntityType.EMPLOYEE, act, entity_id="E001") == "/employees/E001"

    # Missing entity_id raises ValueError
    with pytest.raises(ValueError) as excinfo:
        prof.endpoint_for(EntityType.EMPLOYEE, "get", entity_id=None)
    assert "Action 'get' on EMPLOYEE requires an entity_id" in str(excinfo.value)


def test_generic_profile_endpoint_for_collection_and_fallback_actions():
    """Positive and boundary tests for collection endpoints and fallback actions."""
    prof = GenericProfile()

    # Collection actions
    assert prof.endpoint_for(EntityType.EMPLOYEE, "list") == "/employees"
    assert prof.endpoint_for(EntityType.EMPLOYEE, "create") == "/employees"
    assert prof.endpoint_for(EntityType.EMPLOYEE, "post") == "/employees"

    # Fallback / unrecognised action
    assert prof.endpoint_for(EntityType.EMPLOYEE, "search") == "/employees"

    # Unmapped entity type generates lowercase + 's'
    assert prof.endpoint_for(EntityType.PAYSLIP, "list") == "/payslips"


def test_generic_profile_prepare_headers_auth_and_custom():
    """Positive and boundary tests for header preparation with different auth prefixes."""
    prof = GenericProfile()

    # No auth
    h_none = prof.prepare_headers()
    assert h_none == {"Content-Type": "application/json", "Accept": "application/json"}

    # Raw token without Bearer prefix
    h_raw = prof.prepare_headers(auth_token="token_abc")
    assert h_raw["Authorization"] == "Bearer token_abc"

    # Token with Bearer prefix
    h_bearer = prof.prepare_headers(auth_token="Bearer existing_token")
    assert h_bearer["Authorization"] == "Bearer existing_token"

    # Custom headers merged
    h_custom = prof.prepare_headers(
        auth_token="tok",
        custom_headers={"X-Correlation-ID": "corr-123", "Accept": "application/vnd.api+json"},
    )
    assert h_custom["Authorization"] == "Bearer tok"
    assert h_custom["X-Correlation-ID"] == "corr-123"
    assert h_custom["Accept"] == "application/vnd.api+json"


def test_generic_profile_unwrap_response():
    """Positive and boundary tests for unwrapping nested response envelopes."""
    prof = GenericProfile()

    # data.items envelope
    res1 = prof.unwrap_response({"data": {"items": [{"id": 1}], "total": 1}}, "list", EntityType.EMPLOYEE)
    assert res1 == [{"id": 1}]

    # data direct envelope
    res2 = prof.unwrap_response({"data": {"id": 2, "name": "Alice"}}, "get", EntityType.EMPLOYEE)
    assert res2 == {"id": 2, "name": "Alice"}

    # items root envelope
    res3 = prof.unwrap_response({"items": [{"id": 3}]}, "list", EntityType.EMPLOYEE)
    assert res3 == [{"id": 3}]

    # Non-wrapped dictionary
    res4 = prof.unwrap_response({"id": 4, "status": "active"}, "get", EntityType.EMPLOYEE)
    assert res4 == {"id": 4, "status": "active"}

    # Non-dictionary payload
    res5 = prof.unwrap_response(["item1", "item2"], "list", EntityType.EMPLOYEE)
    assert res5 == ["item1", "item2"]


def test_generic_profile_normalize_payload():
    """Positive test: normalize_payload_for_host returns identical canonical payload."""
    prof = GenericProfile()
    payload = {"full_name": "Alice Bob", "department": "HR"}
    norm = prof.normalize_payload_for_host(payload, EntityType.EMPLOYEE, "create", auth_token="tok")
    assert norm == payload
