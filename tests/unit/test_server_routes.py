"""Integration tests for FastAPI REST routes and MCP Server."""

import pytest
from fastapi.testclient import TestClient
from hrms_plugin.server.app import app
from hrms_plugin.server.mcp_server import HrmsMcpServer


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def mcp_server():
    return HrmsMcpServer()


def test_healthz_endpoint(client):
    """Test health check route."""
    res = client.get("/healthz")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "healthy"
    assert "AE" in data["jurisdictions_supported"]


def test_chat_endpoint_payroll_routing(client):
    """Test /v1/chat endpoint routing with PII masking."""
    payload = {
        "message": "Calculate salary structure for annual CTC 1800000 INR with Aadhaar 9988 7766 5544",
        "jurisdiction": "IN",
        "context": {"annual_ctc": 1800000.0},
        "mask_pii": True,
    }

    res = client.post("/v1/chat", json=payload, headers={"X-Tenant-Id": "T-TEST"})
    assert res.status_code == 200
    data = res.json()
    assert data["intent"] == "PAYROLL_STRUCTURE"
    assert data["routed_agent"] == "StatutoryPayrollAgent"
    assert data["structured_data"]["annual_ctc"] == 1800000.0
    assert len(data["statutory_citations"]) > 0


def test_remediation_execute_endpoint(client):
    """Test /v1/remediate/execute with signed host audit headers."""
    payload = {
        "violation_id": "VIO-PROB-EMP-401",
        "target_entity": "EMPLOYEE",
        "entity_id": "EMP-401",
        "patch_payload": {"probation_days": 180},
        "approver_id": "MGR-007",
        "approver_role": "HR_ADMIN",
    }

    res = client.post("/v1/remediate/execute", json=payload, headers={"X-Tenant-Id": "T-DXB"})
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "APPROVED_AND_QUEUED"
    assert data["audit_tracing"]["X-HITL-Approver-Id"] == "MGR-007"
    assert data["audit_tracing"]["X-Agent-Name"] == "StatutoryComplianceAuditor"


def test_mcp_server_tool_listing_and_invocation(mcp_server):
    """Test MCP server tools discovery and tool execution."""
    tools = mcp_server.list_tools()
    tool_names = [t.name for t in tools]

    assert "hrms_query_labor_statutes" in tool_names
    assert "hrms_calculate_payroll_ctc" in tool_names
    assert "hrms_calculate_uae_eosb" in tool_names
    assert "hrms_audit_compliance" in tool_names

    # Test invoking hrms_calculate_uae_eosb
    res = mcp_server.call_tool(
        "hrms_calculate_uae_eosb",
        {"basic_wage_monthly": 12000.0, "tenure_years": 5.0},
    )
    # 5 yrs * 21 days * 400 AED = 42,000 AED
    assert res["total_eosb_gratuity"] == 42000.0
