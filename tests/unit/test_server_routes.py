"""Integration tests for FastAPI REST routes and MCP Server."""

import pytest

from hrms_plugin.agents.supervisor import SupervisorAgent
from hrms_plugin.server.mcp_server import HrmsMcpServer
from tests.support.scripted_llm import final, tool_call


@pytest.fixture
def client(make_client):
    return make_client()


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


def test_mcp_server_tool_listing_and_invocation():
    """P11: over MCP, number-taking calculators are refused and lookups run through the validated registry."""
    import asyncio

    from hrms_plugin.security.rbac import AuthContext, UserRole

    mcp = HrmsMcpServer(AuthContext(user_id="u", tenant_id="acme", roles=frozenset({UserRole.SUPER_ADMIN})),
                        jurisdiction="AE")
    names = {t.name for t in mcp.list_tools()}
    assert "hrms_search_labor_regulations" in names and "hrms_audit_job_description_bias" in names
    for refused in ("hrms_calculate_eosb_gratuity", "hrms_score_candidate_ats", "hrms_detect_travel_anomaly",
                    "hrms_evaluate_leave_request"):
        assert refused not in names
        assert asyncio.run(mcp.call_tool(refused, {}))["error"] == "TOOL_NOT_AVAILABLE"
    bias = asyncio.run(mcp.call_tool("hrms_audit_job_description_bias", {"jd_text": "Looking for rockstar ninja"}))
    assert bias["has_bias"] is True


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
