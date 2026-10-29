"""Coverage tests for hrms_plugin/connectors/profiles/dynamic.py."""

from __future__ import annotations

from hrms_plugin.connectors.profiles.dynamic import DynamicVendorProfile
from hrms_plugin.schema.canonical import EntityType
from hrms_plugin.schema.introspector import DiscoveredEntity, IntrospectionResult


def test_dynamic_profile_endpoint_for_action_fallbacks():
    """Positive test: Action synonyms (fetch->get, post->create) fallback correctly."""
    prof = DynamicVendorProfile()
    prof.register_endpoint(EntityType.EMPLOYEE, "get", "/api/custom-workers/{id}")
    prof.register_endpoint(EntityType.EMPLOYEE, "create", "/api/custom-workers/new")

    # fetch and read map to get
    assert prof.endpoint_for(EntityType.EMPLOYEE, "fetch", entity_id="123") == "/api/custom-workers/123"
    assert prof.endpoint_for(EntityType.EMPLOYEE, "read", entity_id="456") == "/api/custom-workers/456"

    # post and add map to create
    assert prof.endpoint_for(EntityType.EMPLOYEE, "post") == "/api/custom-workers/new"
    assert prof.endpoint_for(EntityType.EMPLOYEE, "add") == "/api/custom-workers/new"


def test_dynamic_profile_default_rest_convention_fallback():
    """Boundary test: Unmapped entities generate default REST conventions."""
    prof = DynamicVendorProfile()

    # Read / mutate single resource
    assert prof.endpoint_for(EntityType.LEAVE_REQUEST, "get", entity_id="LV-1") == "/leave-requests/LV-1"
    assert prof.endpoint_for(EntityType.LEAVE_REQUEST, "update", entity_id="LV-1") == "/leave-requests/LV-1"
    assert prof.endpoint_for(EntityType.LEAVE_REQUEST, "delete", entity_id="LV-1") == "/leave-requests/LV-1"

    # Collection actions
    assert prof.endpoint_for(EntityType.LEAVE_REQUEST, "list") == "/leave-requests"
    assert prof.endpoint_for(EntityType.LEAVE_REQUEST, "create") == "/leave-requests"


def test_dynamic_profile_kwargs_and_param_interpolation():
    """Positive test: entity_id variants and custom kwargs are interpolated in route template."""
    prof = DynamicVendorProfile()
    prof.register_endpoint(
        EntityType.EMPLOYEE,
        "get",
        "/orgs/{org_id}/departments/{dept}/employees/{employee_id}",
    )

    path = prof.endpoint_for(
        EntityType.EMPLOYEE,
        "get",
        entity_id="EMP-99",
        org_id="corp-1",
        dept="finance",
    )
    assert path == "/orgs/corp-1/departments/finance/employees/EMP-99"

    # Also test {employeeId} camelCase placeholder
    prof.register_endpoint(EntityType.EMPLOYEE, "patch", "/staff/{employeeId}")
    assert prof.endpoint_for(EntityType.EMPLOYEE, "patch", entity_id="E77") == "/staff/E77"


def test_dynamic_profile_prepare_headers_and_custom_auth_format():
    """Positive test: prepare_headers formats custom auth headers and merges custom headers."""
    prof = DynamicVendorProfile(
        auth_header_format="Token key={token}",
        auth_header_name="X-Auth-Token",
    )

    headers = prof.prepare_headers(
        auth_token="secret_value",
        custom_headers={"X-Tenant": "tenant_a"},
    )
    assert headers["X-Auth-Token"] == "Token key=secret_value"
    assert headers["X-Tenant"] == "tenant_a"
    assert headers["Content-Type"] == "application/json"
    assert headers["Accept"] == "application/json"


def test_dynamic_profile_unwrap_response_variations():
    """Positive and boundary tests for unwrapping various wrapper keys and structures."""
    prof = DynamicVendorProfile()

    # Wrapper key with nested items
    assert prof.unwrap_response({"data": {"items": [10, 20]}}, "list", EntityType.EMPLOYEE) == [10, 20]

    # Wrapper key with direct payload (no nested items)
    assert prof.unwrap_response({"records": {"count": 1, "status": "ok"}}, "get", EntityType.EMPLOYEE) == {
        "count": 1,
        "status": "ok",
    }
    assert prof.unwrap_response({"rows": [1, 2, 3]}, "list", EntityType.EMPLOYEE) == [1, 2, 3]

    # No wrapper key matches
    assert prof.unwrap_response({"other": "value"}, "get", EntityType.EMPLOYEE) == {"other": "value"}

    # Non-dictionary
    assert prof.unwrap_response(42, "get", EntityType.EMPLOYEE) == 42


def test_dynamic_profile_normalize_payload():
    """Positive test: normalize_payload_for_host preserves canonical payload."""
    prof = DynamicVendorProfile()
    data = {"key": "val"}
    assert prof.normalize_payload_for_host(data, EntityType.EMPLOYEE, "create") == data


def test_dynamic_profile_from_introspection_branches():
    """Positive test: from_introspection handles endpoints with and without URL parameters."""
    intro = IntrospectionResult(
        title="Dynamic Test Spec",
        version="1.0.0",
        endpoints_count=3,
        entities={
            "employee": DiscoveredEntity(
                name="employee",
                schema={},
                read_endpoint="GET /api/v1/employees",
                write_endpoint="POST /api/v1/employees/{id}",
            ),
        },
    )

    prof = DynamicVendorProfile.from_introspection(intro, vendor_name="introspected_vendor")
    assert prof.vendor_name == "introspected_vendor"
    assert prof.endpoint_for(EntityType.EMPLOYEE, "list") == "/api/v1/employees"
    assert prof.endpoint_for(EntityType.EMPLOYEE, "get", entity_id="10") == "/api/v1/employees/10"
    assert prof.endpoint_for(EntityType.EMPLOYEE, "update", entity_id="10") == "/api/v1/employees/10"


def test_dynamic_profile_from_introspection_with_parameterized_read():
    """Positive test: from_introspection handles read_endpoint containing path parameters."""
    intro = IntrospectionResult(
        title="Param Read Spec",
        version="1.0.0",
        endpoints_count=2,
        entities={
            "employee": DiscoveredEntity(
                name="employee",
                schema={},
                read_endpoint="GET /api/v1/staff/{employee_id}",
                write_endpoint="POST /api/v1/staff",
            ),
        },
    )

    prof = DynamicVendorProfile.from_introspection(intro)
    assert prof.endpoint_for(EntityType.EMPLOYEE, "get", entity_id="99") == "/api/v1/staff/99"
    assert prof.endpoint_for(EntityType.EMPLOYEE, "list") == "/api/v1/staff"
    assert prof.endpoint_for(EntityType.EMPLOYEE, "create") == "/api/v1/staff"
    assert prof.endpoint_for(EntityType.EMPLOYEE, "update", entity_id="99") == "/api/v1/staff/99"

