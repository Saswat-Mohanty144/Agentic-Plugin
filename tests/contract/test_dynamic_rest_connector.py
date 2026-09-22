"""Contract tests for DynamicRESTConnector and IceiptsProfile using httpx.MockTransport."""

import json

import httpx
import pytest

from hrms_plugin.connectors.base import (
    ConnectorConfig,
    HRMSAuthError,
    HRMSNotFoundError,
    HRMSValidationError,
)
from hrms_plugin.connectors.dynamic_rest import DynamicRESTConnector
from hrms_plugin.connectors.profiles.iceipts import IceiptsProfile
from hrms_plugin.schema.canonical import (
    CanonicalEmployee,
    CanonicalRequisition,
    EntityType,
)
from hrms_plugin.schema.mapping import EntityMapping, FieldMap


@pytest.mark.asyncio
async def test_iceipts_create_requisition_contract():
    """Verify that creating an iCeipts requisition correctly formats the payload and wraps the response."""
    captured_requests: list[httpx.Request] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        assert request.method == "POST"
        assert "/job/createRequisition/usr-recruiter-888" in str(request.url)

        body = json.loads(request.content.decode("utf-8"))
        # Verify iCeipts wire conventions are preserved
        assert body["employeeId"] == "usr-recruiter-888"
        assert body["jobData"]["recuiterId"] == "usr-recruiter-888"  # vendor typo check
        assert body["jobData"]["jobMode"] == "WorkFromHome"
        assert body["jobData"]["type"] == "Full-time"
        assert body["jobData"]["title"] == "Senior Platform Architect"

        response_data = {
            "success": True,
            "data": {
                "job": {
                    "id": "job-uuid-9999",
                    "title": "Senior Platform Architect",
                    "status": "OPEN",
                    "version": "1",
                }
            },
        }
        return httpx.Response(201, json=response_data)

    transport = httpx.MockTransport(mock_handler)
    client = httpx.AsyncClient(transport=transport, base_url="http://localhost:3000")

    config = ConnectorConfig(base_url="http://localhost:3000", vendor_name="iceipts")
    profile = IceiptsProfile(default_user_id="usr-recruiter-888")

    async with DynamicRESTConnector(config=config, profile=profile, client=client) as connector:
        canonical_req = {
            "title": "Senior Platform Architect",
            "department": "Infrastructure",
            "work_model": "remote",
            "employment_type": "full_time",
            "min_salary": 250000,
            "max_salary": 350000,
            "recruiter_id": "usr-recruiter-888",
        }

        created = await connector.create_entity(
            entity_type=EntityType.REQUISITION,
            canonical_payload=canonical_req,
            user_id="usr-recruiter-888",
        )

        assert isinstance(created, CanonicalRequisition)
        assert created.title == "Senior Platform Architect"
        assert created.source_ref.external_id == "job-uuid-9999"
        assert created.source_ref.vendor == "iceipts"


@pytest.mark.asyncio
async def test_iceipts_fetch_requisition_detail():
    """Verify fetching an iCeipts requisition correctly extracts nested job details."""

    def mock_handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert "/job/detail/job-uuid-42" in str(request.url)
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "job": {
                        "id": "job-uuid-42",
                        "title": "Principal AI Researcher",
                        "department": "R&D",
                        "status": "OPEN",
                    }
                },
            },
        )

    transport = httpx.MockTransport(mock_handler)
    client = httpx.AsyncClient(transport=transport, base_url="http://localhost:3000")

    config = ConnectorConfig(base_url="http://localhost:3000", vendor_name="iceipts")
    profile = IceiptsProfile()

    async with DynamicRESTConnector(config=config, profile=profile, client=client) as connector:
        entity = await connector.fetch_entity(
            entity_type=EntityType.REQUISITION,
            entity_id="job-uuid-42",
        )

        assert isinstance(entity, CanonicalRequisition)
        assert entity.title == "Principal AI Researcher"
        assert entity.source_ref.external_id == "job-uuid-42"


@pytest.mark.asyncio
async def test_dynamic_rest_connector_with_schema_mapping():
    """Verify that an EntityMapping converts custom vendor field names into Canonical attributes."""

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "c_emp_id": "EMP-007",
                    "c_first_name": "Fatima",
                    "c_last_name": "Al-Zahra",
                    "c_work_email": "fatima@tech.ae",
                    "c_title": "Head of People",
                }
            },
        )

    transport = httpx.MockTransport(mock_handler)
    client = httpx.AsyncClient(transport=transport, base_url="http://mock-hrms.local")

    # Define dynamic schema mapping for Employee
    mapping = EntityMapping(
        vendor="generic",
        entity_type="EMPLOYEE",
        id_path="c_emp_id",
        fields=(
            FieldMap(target="work_email", source="c_work_email", required=True),
            FieldMap(target="first_name", source="c_first_name"),
            FieldMap(target="last_name", source="c_last_name"),
            FieldMap(target="designation", source="c_title"),
        ),
    )

    config = ConnectorConfig(base_url="http://mock-hrms.local", vendor_name="generic")

    async with DynamicRESTConnector(
        config=config,
        mappings={EntityType.EMPLOYEE: mapping},
        client=client,
    ) as connector:
        emp = await connector.fetch_entity(
            entity_type=EntityType.EMPLOYEE,
            entity_id="EMP-007",
        )

        assert isinstance(emp, CanonicalEmployee)
        assert emp.source_ref.external_id == "EMP-007"
        assert emp.work_email == "fatima@tech.ae"
        assert emp.designation == "Head of People"


@pytest.mark.asyncio
async def test_dynamic_rest_connector_retry_on_503():
    """Verify that transient 503 errors trigger automatic retries and succeed on subsequent attempt."""
    attempts = 0

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, json={"error": "Service Temporarily Unavailable"})
        return httpx.Response(200, json={"data": {"id": "emp_1", "name": "John"}})

    transport = httpx.MockTransport(mock_handler)
    client = httpx.AsyncClient(transport=transport, base_url="http://localhost:3000")

    config = ConnectorConfig(
        base_url="http://localhost:3000",
        max_retries=3,
        backoff_factor=0.01,  # fast test backoff
    )

    async with DynamicRESTConnector(config=config, client=client) as connector:
        emp = await connector.fetch_entity(
            entity_type=EntityType.EMPLOYEE,
            entity_id="emp_1",
        )
        assert attempts == 2
        assert emp.source_ref.external_id == "emp_1"


@pytest.mark.asyncio
async def test_dynamic_rest_connector_error_hierarchy():
    """Verify structured exceptions for 401, 404, and 400."""

    def mock_handler(request: httpx.Request) -> httpx.Response:
        path = str(request.url.path)
        if "unauthorized" in path:
            return httpx.Response(401, json={"message": "Invalid Bearer Token"})
        elif "not-found" in path:
            return httpx.Response(404, json={"message": "Candidate record does not exist"})
        elif "bad-request" in path:
            return httpx.Response(400, json={"error_message": "departmentId must be a valid UUID"})
        return httpx.Response(500, json={"error": "Internal Error"})

    transport = httpx.MockTransport(mock_handler)
    client = httpx.AsyncClient(transport=transport, base_url="http://localhost:3000")
    config = ConnectorConfig(base_url="http://localhost:3000")

    async with DynamicRESTConnector(config=config, client=client) as connector:
        # 401
        with pytest.raises(HRMSAuthError) as auth_err:
            await connector.fetch_entity(EntityType.EMPLOYEE, "unauthorized")
        assert auth_err.value.status_code == 401
        assert "Invalid Bearer Token" in str(auth_err.value)

        # 404
        with pytest.raises(HRMSNotFoundError) as nf_err:
            await connector.fetch_entity(EntityType.CANDIDATE, "not-found")
        assert nf_err.value.status_code == 404
        assert "Candidate record does not exist" in str(nf_err.value)

        # 400
        with pytest.raises(HRMSValidationError) as val_err:
            await connector.fetch_entity(EntityType.EMPLOYEE, "bad-request")
        assert val_err.value.status_code == 400
        assert "departmentId must be a valid UUID" in str(val_err.value)
