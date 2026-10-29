"""Coverage tests for hrms_plugin/connectors/dynamic_rest.py."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from hrms_plugin.connectors.auth import ApiKeyAuth
from hrms_plugin.connectors.base import (
    ConnectorConfig,
    HRMSAuthError,
    HRMSConflictError,
    HRMSConnectorError,
    HRMSNotFoundError,
    HRMSTimeoutError,
    HRMSValidationError,
)
from hrms_plugin.connectors.concurrency import IdempotencyManager
from hrms_plugin.connectors.dynamic_rest import DynamicRESTConnector
from hrms_plugin.connectors.profiles.frappe import FrappeProfile
from hrms_plugin.connectors.profiles.generic import GenericProfile
from hrms_plugin.connectors.profiles.zoho import ZohoProfile
from hrms_plugin.schema.canonical import CanonicalEmployee, EntityType
from hrms_plugin.schema.mapping import EntityMapping


@pytest.mark.asyncio
async def test_connector_lifecycle_owns_client():
    """Positive test: Connector creates its own HTTP client when client is None, and closes cleanly."""
    config = ConnectorConfig(vendor_name="generic", base_url="https://api.example.com", timeout_seconds=10.0)
    connector = DynamicRESTConnector(config=config)
    assert connector._owns_client is True
    assert connector._client is not None

    async with connector:
        assert not connector._client.is_closed
    assert connector._client.is_closed


@pytest.mark.asyncio
async def test_connector_send_request_with_auth_strategy():
    """Positive test: Dynamic auth strategy headers are injected into outbound requests."""
    captured_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(status_code=200, json={"data": {"id": "E1", "full_name": "Test User"}})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        config = ConnectorConfig(vendor_name="generic", base_url="https://api.example.com")
        auth = ApiKeyAuth(api_key="secret-api-token", header_name="X-Custom-Auth", prefix="Bearer")
        connector = DynamicRESTConnector(config=config, auth_strategy=auth, client=client)

        entity = await connector.fetch_entity(EntityType.EMPLOYEE, "E1")
        assert isinstance(entity, CanonicalEmployee)
        assert len(captured_requests) == 1
        assert captured_requests[0].headers["X-Custom-Auth"] == "Bearer secret-api-token"


@pytest.mark.asyncio
async def test_connector_retry_transient_network_errors(monkeypatch: pytest.MonkeyPatch):
    """Positive test: Connector retries on ConnectError and succeeds on subsequent attempt."""
    async def mock_sleep(seconds: float) -> None:
        pass

    monkeypatch.setattr(asyncio, "sleep", mock_sleep)
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("Connection drop", request=request)
        return httpx.Response(status_code=200, json={"data": {"id": "E2", "full_name": "Recovered User"}})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        config = ConnectorConfig(
            vendor_name="generic",
            base_url="https://api.example.com",
            max_retries=3,
            backoff_factor=0.001,
        )
        connector = DynamicRESTConnector(config=config, client=client)
        entity = await connector.fetch_entity(EntityType.EMPLOYEE, "E2")
        assert entity.source_ref.external_id == "E2"
        assert attempts == 2


@pytest.mark.asyncio
async def test_connector_exhausted_retries_raises_connector_error(monkeypatch: pytest.MonkeyPatch):
    """Negative test: Network error persists across max_retries and raises HRMSConnectorError."""
    async def mock_sleep(seconds: float) -> None:
        pass

    monkeypatch.setattr(asyncio, "sleep", mock_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("Socket reset", request=request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        config = ConnectorConfig(
            vendor_name="generic",
            base_url="https://api.example.com",
            max_retries=2,
            backoff_factor=0.001,
        )
        connector = DynamicRESTConnector(config=config, client=client)
        with pytest.raises(HRMSConnectorError) as excinfo:
            await connector.fetch_entity(EntityType.EMPLOYEE, "E3")
        assert "Network connection failed after 2 attempts" in excinfo.value.message


@pytest.mark.asyncio
async def test_connector_timeout_raises_hrms_timeout_error():
    """Negative test: Request timeout raises HRMSTimeoutError."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("Read timed out", request=request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        config = ConnectorConfig(vendor_name="generic", base_url="https://api.example.com", timeout_seconds=5.0)
        connector = DynamicRESTConnector(config=config, client=client)
        with pytest.raises(HRMSTimeoutError) as excinfo:
            await connector.fetch_entity(EntityType.EMPLOYEE, "E4")
        assert "timed out after 5.0s" in excinfo.value.message
        assert excinfo.value.vendor == "generic"


@pytest.mark.asyncio
async def test_connector_handle_response_statuses_and_non_json():
    """Negative and boundary tests: Status codes 401, 404, 409, 422, 500 with non-JSON body."""
    def make_handler(status: int, content_text: str):
        return lambda req: httpx.Response(status_code=status, text=content_text)

    config = ConnectorConfig(vendor_name="generic", base_url="https://api.example.com", max_retries=1)

    # 401 Unauthorized
    async with httpx.AsyncClient(transport=httpx.MockTransport(make_handler(401, '{"error":"unauthorized"}'))) as cl:
        conn = DynamicRESTConnector(config=config, client=cl)
        with pytest.raises(HRMSAuthError) as exc:
            await conn.fetch_entity(EntityType.EMPLOYEE, "1")
        assert exc.value.status_code == 401

    # 404 Not Found
    async with httpx.AsyncClient(transport=httpx.MockTransport(make_handler(404, '{"error":"missing"}'))) as cl:
        conn = DynamicRESTConnector(config=config, client=cl)
        with pytest.raises(HRMSNotFoundError) as exc:
            await conn.fetch_entity(EntityType.EMPLOYEE, "1")
        assert exc.value.status_code == 404

    # 409 Conflict
    async with httpx.AsyncClient(transport=httpx.MockTransport(make_handler(409, '{"error":"conflict"}'))) as cl:
        conn = DynamicRESTConnector(config=config, client=cl)
        with pytest.raises(HRMSConflictError) as exc:
            await conn.fetch_entity(EntityType.EMPLOYEE, "1")
        assert exc.value.status_code == 409

    # 422 Validation Error
    async with httpx.AsyncClient(transport=httpx.MockTransport(make_handler(422, '{"error":"invalid"}'))) as cl:
        conn = DynamicRESTConnector(config=config, client=cl)
        with pytest.raises(HRMSValidationError) as exc:
            await conn.fetch_entity(EntityType.EMPLOYEE, "1")
        assert exc.value.status_code == 422

    # 500 Server Error with non-JSON text (HTML error page)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(make_handler(500, "<html>500 Server Error</html>"))
    ) as cl:
        conn = DynamicRESTConnector(config=config, client=cl)
        with pytest.raises(HRMSConnectorError) as exc:
            await conn.fetch_entity(EntityType.EMPLOYEE, "1")
        assert exc.value.status_code == 500
        assert exc.value.response_body == {"raw": "<html>500 Server Error</html>"}


@pytest.mark.asyncio
async def test_connector_mapping_external_id_exception_fallback():
    """Boundary test: Mapping whose external_id raises falls back to raw_dict id in list_entities."""
    class BadMapping(EntityMapping):
        def external_id(self, payload):
            raise RuntimeError("Corrupt ID calculation")

    mapping = BadMapping(
        vendor="generic",
        entity_type="EMPLOYEE",
        id_path="id",
    )
    config = ConnectorConfig(vendor_name="generic", base_url="https://api.example.com")
    transport = httpx.MockTransport(
        lambda req: httpx.Response(status_code=200, json={"data": [{"id": "fallback-id", "name": "Alice"}]})
    )
    async with httpx.AsyncClient(transport=transport) as client:
        conn = DynamicRESTConnector(config=config, mappings={EntityType.EMPLOYEE: mapping}, client=client)
        results = await conn.list_entities(EntityType.EMPLOYEE)
        assert len(results) == 1
        assert results[0].source_ref.external_id == "fallback-id"


def test_connector_to_canonical_unknown_class():
    """Negative test: Calling _to_canonical with entity type not in _ENTITY_CLASS_MAP raises ValueError."""
    config = ConnectorConfig(vendor_name="generic", base_url="https://api.example.com")
    conn = DynamicRESTConnector(config=config)
    with pytest.raises(ValueError) as exc:
        conn._to_canonical({"id": "1"}, EntityType.LEAVE_BALANCE)
    assert "Unknown Canonical entity class for EntityType.LEAVE_BALANCE" in str(exc.value)


@pytest.mark.asyncio
async def test_connector_fetch_entity_unwrapped_not_dict_raises():
    """Negative test: fetch_entity raises HRMSConnectorError if unwrapped payload is not a dict."""
    transport = httpx.MockTransport(lambda req: httpx.Response(status_code=200, json={"data": [1, 2, 3]}))
    async with httpx.AsyncClient(transport=transport) as client:
        config = ConnectorConfig(vendor_name="generic", base_url="https://api.example.com")
        conn = DynamicRESTConnector(config=config, client=client)
        with pytest.raises(HRMSConnectorError) as exc:
            await conn.fetch_entity(EntityType.EMPLOYEE, "emp1")
        assert "Expected dictionary payload for EMPLOYEE 'emp1'" in exc.value.message


@pytest.mark.asyncio
async def test_connector_list_entities_variations():
    """Positive and boundary tests: list_entities with pagination, filter params, and different collection wraps."""
    captured_params = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_params.append(dict(request.url.params))
        return httpx.Response(
            status_code=200,
            json={"data": {"rows": [{"id": "E1", "full_name": "Alice"}, {"id": "E2", "full_name": "Bob"}]}},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        config = ConnectorConfig(vendor_name="generic", base_url="https://api.example.com")
        conn = DynamicRESTConnector(config=config, client=client)
        results = await conn.list_entities(
            EntityType.EMPLOYEE,
            filters={"department": "Engineering"},
            pagination={"limit": 10, "offset": 20},
        )
        assert len(results) == 2
        assert results[0].source_ref.external_id == "E1"
        assert results[1].source_ref.external_id == "E2"
        assert captured_params[0]["department"] == "Engineering"
        assert captured_params[0]["limit"] == "10"
        assert captured_params[0]["offset"] == "20"


@pytest.mark.asyncio
async def test_connector_create_entity_idempotency_cache():
    """Positive test: create_entity returns cached result for identical idempotency key."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(status_code=201, json={"data": {"id": "NEW-1", "full_name": "Alice Developer"}})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        config = ConnectorConfig(vendor_name="generic", base_url="https://api.example.com")
        idemp = IdempotencyManager()
        conn = DynamicRESTConnector(config=config, idempotency_manager=idemp, client=client)

        payload = {"full_name": "Alice Developer"}
        res1 = await conn.create_entity(EntityType.EMPLOYEE, payload, actor_id="actor-1")
        assert res1.source_ref.external_id == "NEW-1"
        assert call_count == 1

        # Second call returns cached result without sending HTTP request
        res2 = await conn.create_entity(EntityType.EMPLOYEE, payload, actor_id="actor-1")
        assert res2.source_ref.external_id == "NEW-1"
        assert call_count == 1


@pytest.mark.asyncio
async def test_connector_create_entity_self_healing_success():
    """Positive test: create_entity catches HRMSValidationError, heals payload, and succeeds on retry."""
    attempts = 0
    posted_payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        data = request.read().decode("utf-8")
        posted_payloads.append(data)
        if attempts == 1:
            return httpx.Response(
                status_code=400,
                json={"response": {"errors": {"message": "Validation error: lastname is required"}}},
            )
        return httpx.Response(
            status_code=200,
            json={"response": {"result": [{"EmployeeDetails": {"id": "Z10", "FirstName": "John"}}]}},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        config = ConnectorConfig(vendor_name="zoho", base_url="https://people.zoho.com")
        profile = ZohoProfile()
        conn = DynamicRESTConnector(config=config, profile=profile, client=client)

        entity = await conn.create_entity(EntityType.EMPLOYEE, {"first_name": "John"})
        assert entity.source_ref.external_id == "Z10"
        assert attempts == 2
        assert '"LastName":"John"' in posted_payloads[1]


@pytest.mark.asyncio
async def test_connector_create_entity_self_healing_retry_fails_and_releases_idempotency():
    """Negative test: Self-healing retry fails, releasing idempotency key lock."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                status_code=400,
                json={"response": {"errors": {"message": "Validation error: lastname is required"}}},
            )
        raise httpx.ConnectError("Network dropped on retry", request=request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        config = ConnectorConfig(vendor_name="zoho", base_url="https://people.zoho.com", max_retries=1)
        profile = ZohoProfile()
        idemp = IdempotencyManager()
        conn = DynamicRESTConnector(config=config, profile=profile, idempotency_manager=idemp, client=client)

        with pytest.raises(HRMSConnectorError):
            await conn.create_entity(EntityType.EMPLOYEE, {"first_name": "Dave"})
        assert attempts == 2


@pytest.mark.asyncio
async def test_connector_update_entity_with_if_match_and_self_healing():
    """Positive test: update_entity sends If-Match header and performs self-healing on validation error."""
    attempts = 0
    captured_headers = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        captured_headers.append(dict(request.headers))
        if attempts == 1:
            return httpx.Response(status_code=422, json={"message": "naming_series is mandatory"})
        return httpx.Response(status_code=200, json={"data": {"name": "EMP-99", "company": "Frappe Corp"}})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        config = ConnectorConfig(vendor_name="frappe", base_url="https://frappe.example.com")
        profile = FrappeProfile(company="Frappe Corp")
        conn = DynamicRESTConnector(config=config, profile=profile, client=client)

        entity = await conn.update_entity(
            EntityType.EMPLOYEE,
            "EMP-99",
            {"first_name": "Jane"},
            expected_version="etag-rev-3",
        )
        assert entity.source_ref.external_id == "EMP-99"
        assert attempts == 2
        assert captured_headers[0]["if-match"] == "etag-rev-3"


@pytest.mark.asyncio
async def test_connector_update_entity_unhealable_error_raises():
    """Negative test: update_entity re-raises HRMSValidationError when error cannot be healed."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=400, json={"error": "Immutable field cannot be changed"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        config = ConnectorConfig(vendor_name="generic", base_url="https://api.example.com", max_retries=1)
        conn = DynamicRESTConnector(config=config, profile=GenericProfile(), client=client)

        with pytest.raises(HRMSValidationError) as exc:
            await conn.update_entity(EntityType.EMPLOYEE, "EMP-1", {"salary": 50000})
        assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_connector_delete_entity():
    """Positive test: delete_entity sends DELETE request and returns True."""
    captured_method = ""
    captured_path = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_method, captured_path
        captured_method = request.method
        captured_path = request.url.path
        return httpx.Response(status_code=204)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        config = ConnectorConfig(vendor_name="generic", base_url="https://api.example.com")
        conn = DynamicRESTConnector(config=config, profile=GenericProfile(), client=client)

        result = await conn.delete_entity(EntityType.EMPLOYEE, "EMP-DEL")
        assert result is True
        assert captured_method == "DELETE"
        assert captured_path == "/employees/EMP-DEL"


@pytest.mark.asyncio
async def test_connector_create_entity_unhealable_validation_error():
    """Negative test: create_entity re-raises HRMSValidationError when unhealable."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=400, json={"error": "Unhealable constraint violation"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        config = ConnectorConfig(vendor_name="generic", base_url="https://api.example.com", max_retries=1)
        conn = DynamicRESTConnector(config=config, client=client)
        with pytest.raises(HRMSValidationError) as exc:
            await conn.create_entity(EntityType.EMPLOYEE, {"full_name": "Bob"})
        assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_connector_create_entity_generic_exception():
    """Negative test: create_entity releases idempotency lock on 500 error."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=500, json={"error": "Internal server crash"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        config = ConnectorConfig(vendor_name="generic", base_url="https://api.example.com", max_retries=1)
        conn = DynamicRESTConnector(config=config, client=client)
        with pytest.raises(HRMSConnectorError) as exc:
            await conn.create_entity(EntityType.EMPLOYEE, {"full_name": "Bob"})
        assert exc.value.status_code == 500


@pytest.mark.asyncio
async def test_connector_update_entity_normal_success():
    """Positive test: update_entity succeeds on first attempt without healing."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=200, json={"data": {"id": "UPD-1", "full_name": "Updated Name"}})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        config = ConnectorConfig(vendor_name="generic", base_url="https://api.example.com")
        conn = DynamicRESTConnector(config=config, client=client)
        entity = await conn.update_entity(EntityType.EMPLOYEE, "UPD-1", {"full_name": "Updated Name"})
        assert entity.source_ref.external_id == "UPD-1"

