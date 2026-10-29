"""Integration tests for FastAPI REST routes and MCP Server."""

import pytest

from hrms_plugin.agents.supervisor import SupervisorAgent
from hrms_plugin.server.mcp_server import HrmsMcpServer
from tests.support.scripted_llm import final, tool_call


@pytest.fixture
def client(make_client):
    return make_client()


@pytest.fixture
def mcp_server():
    return HrmsMcpServer()


def test_healthz_endpoint(client):
    """Test health check route."""
    res = client.get("/healthz")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "healthy"


def test_chat_endpoint_payroll_routing_non_agentic(client, bearer):
    """AM1: original assertions, on the non-agentic path they were written for (with PII masking)."""
    payload = {
        "message": "Calculate salary structure for annual CTC 1800000 INR with Aadhaar 9988 7766 5544",
        "jurisdiction": "IN",
        "context": {"annual_ctc": 1800000.0},
        "mask_pii": True,
        "agentic": False,
    }
    res = client.post("/v1/chat", json=payload, headers=bearer(roles=["hr_manager"]))
    assert res.status_code == 200
    data = res.json()
    assert data["intent"] == "PAYROLL_STRUCTURE"
    assert data["routed_agent"] == "StatutoryPayrollAgent"
    assert data["structured_data"]["annual_ctc"] == 1800000.0
    assert len(data["statutory_citations"]) > 0


def test_chat_endpoint_payroll_routing_agentic(make_client, bearer, scripted):
    """AM1: the agentic path reports the intent of the tool it ran."""
    gateway, _ = scripted(tool_call("calculate_salary_structure", gross_or_ctc=1800000.0), final("Structured."))
    client = make_client(supervisor_agent=SupervisorAgent(llm_gateway=gateway))
    res = client.post("/v1/chat", headers=bearer(roles=["hr_manager"]),
                      json={"message": "Calculate salary structure for annual CTC 1800000", "jurisdiction": "IN"})
    assert res.status_code == 200
    data = res.json()
    assert data["intent"] == "PAYROLL_STRUCTURE"
    assert data["routed_agent"] == "AgenticBrain"
    assert data["tool_calls"][0]["arguments"]["jurisdiction"] == "IN"


def test_remediation_execute_endpoint(client, bearer):
    """Test /v1/remediate/execute with signed host audit headers."""
    payload = {
        "violation_id": "VIO-PROB-EMP-401",
        "target_entity": "EMPLOYEE",
        "entity_id": "EMP-401",
        "patch_payload": {"probation_days": 180},
    }
    res = client.post("/v1/remediate/execute", json=payload, headers=bearer(sub="MGR-007", roles=["hr_manager"]))
    assert res.status_code == 200
    data = res.json()
    # Nothing is written by this endpoint; it must not claim otherwise (agy D-F2). Writes go through proposals.
    assert data["status"] == "ATTRIBUTION_ONLY" and data["applied"] is False
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


def test_generate_requisition_archetypes_endpoint(client, bearer):
    """Test /api/requisitions/generate-archetypes endpoint called by hrms-next."""
    payload = {
        "prompt": "Senior Full Stack React and Python Engineer",
        "location": "Bangalore / Remote",
    }
    res = client.post("/api/requisitions/generate-archetypes", json=payload, headers=bearer(roles=["recruiter"]))
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert len(data["archetypes"]) == 3
    first = data["archetypes"][0]
    assert "jobData" in first
    assert "React" in first["jobData"]["requirements"] or "Python" in first["jobData"]["requirements"]
    assert first["jobData"]["location"] == "Bangalore / Remote"
    assert first["jobData"]["minSalary"] is None  # P8: pay is left to the recruiter, never invented
