"""Tools are offered and executed only with the caller's permission (spec §6.5)."""

import logging

import pytest

from hrms_plugin.agents.orchestrator import AgenticOrchestrator
from hrms_plugin.agents.supervisor import UserIntent
from hrms_plugin.agents.tools import AgenticToolRegistry
from hrms_plugin.ai.llm import ToolDefinition
from hrms_plugin.security.rbac import ROLE_PERMISSIONS, AuthContext
from hrms_plugin.security.rbac import Permission as P
from hrms_plugin.security.rbac import UserRole as R
from tests.support.scripted_llm import final, tool_call

TOOL_PERMISSIONS = {
    "calculate_salary_structure": P.VIEW_SALARY, "calculate_eosb_gratuity": P.VIEW_SALARY,
    "audit_offboarding_fnf_timeline": P.VIEW_SALARY, "compute_final_settlement": P.VIEW_SALARY,
    "audit_statutory_compliance": P.VIEW_COMPLIANCE, "evaluate_leave_request": P.APPLY_LEAVE,
    "detect_travel_anomaly": P.APPROVE_LEAVE, "score_candidate_ats": P.MANAGE_RECRUITMENT,
    "audit_job_description_bias": P.MANAGE_RECRUITMENT, "evaluate_onboarding_offer": P.MANAGE_RECRUITMENT,
    "generate_requisition_archetypes": P.MANAGE_RECRUITMENT, "verify_onboarding_documents": P.VIEW_EMPLOYEE,
    "search_labor_regulations": P.USE_COPILOT, "audit_travel_expense_claim": P.APPROVE_EXPENSE,
    "calculate_pf_contribution": P.VIEW_SALARY,
}


def auth(*roles):
    return AuthContext(user_id="u-1", tenant_id="acme", roles=frozenset(roles))


@pytest.mark.parametrize("tool,perm", sorted(TOOL_PERMISSIONS.items()))
def test_tool_permission_table(tool, perm):
    assert AgenticToolRegistry().permission_for(tool) is perm


def test_every_registered_tool_has_a_permission():
    reg = AgenticToolRegistry()
    names = {d.name for d in reg.get_definitions()}
    assert names == set(TOOL_PERMISSIONS)
    assert all(isinstance(reg.permission_for(name), P) for name in names)


def test_register_without_permission_raises():
    with pytest.raises(TypeError):
        AgenticToolRegistry().register(ToolDefinition(name="x", description="", parameters={}), lambda: {},
                                       intent=UserIntent.GENERAL_HR)


@pytest.mark.parametrize("role", list(R))
def test_offered_set_per_role(role):
    offered = {d.name for d in AgenticToolRegistry().definitions_for(auth(role))}
    assert offered == {t for t, p in TOOL_PERMISSIONS.items() if p in ROLE_PERMISSIONS[role]}


def test_employee_offered_set_is_exact():
    offered = {d.name for d in AgenticToolRegistry().definitions_for(auth(R.EMPLOYEE))}
    assert offered == {"evaluate_leave_request", "verify_onboarding_documents", "search_labor_regulations"}


async def test_unoffered_tool_call_is_blocked_and_logged(scripted, caplog):
    caplog.set_level(logging.WARNING)
    gateway, client = scripted(tool_call("audit_statutory_compliance"), final("ok"))
    orch = AgenticOrchestrator(llm_gateway=gateway, tool_registry=AgenticToolRegistry())
    resp = await orch.run(user_message="audit", jurisdiction="AE", auth=auth(R.EMPLOYEE))
    assert resp.structured_data is None
    assert resp.intent == UserIntent.GENERAL_HR
    assert "TOOL_NOT_PERMITTED" in client.calls[1]["messages"][-1].content
    rec = next(r for r in caplog.records if getattr(r, "reason", "") == "TOOL_NOT_PERMITTED")
    assert (rec.sub, rec.tenant, rec.tool) == ("u-1", "acme", "audit_statutory_compliance")


async def test_zero_roles_calls_llm_without_tools(scripted):
    gateway, client = scripted(final("hello"))
    orch = AgenticOrchestrator(llm_gateway=gateway, tool_registry=AgenticToolRegistry())
    await orch.run(user_message="hi", jurisdiction="AE", auth=auth())
    assert client.calls[0]["tools"] is None


async def test_stream_filters_and_rechecks(scripted):
    gateway, client = scripted(tool_call("audit_statutory_compliance"), final("ok"))
    orch = AgenticOrchestrator(llm_gateway=gateway, tool_registry=AgenticToolRegistry())
    events = [e async for e in orch.run_stream(user_message="a", jurisdiction="AE", auth=auth(R.EMPLOYEE))]
    offered = {t.name for t in client.calls[0]["tools"]}
    assert "audit_statutory_compliance" not in offered
    obs = next(e for e in events if e["event"] == "tool_observation")
    assert obs["data"]["output"] == {"error": "TOOL_NOT_PERMITTED", "tool": "audit_statutory_compliance"}


async def test_tenant_injected_from_auth(scripted):
    gateway, _ = scripted(tool_call("audit_statutory_compliance", tenant_id="evil"), final("ok"))
    orch = AgenticOrchestrator(llm_gateway=gateway, tool_registry=AgenticToolRegistry())
    resp = await orch.run(user_message="a", jurisdiction="AE", auth=auth(R.HR_ADMIN))
    assert resp.tool_calls[0]["arguments"]["tenant_id"] == "acme"


def test_http_employee_chat_cannot_run_compliance_audit(make_client, bearer, scripted):
    from hrms_plugin.agents.supervisor import SupervisorAgent

    gateway, _ = scripted(tool_call("audit_statutory_compliance"), final("ok"))
    client = make_client(supervisor_agent=SupervisorAgent(llm_gateway=gateway))
    res = client.post("/v1/chat", headers=bearer(roles=["staff"]), json={"message": "audit", "jurisdiction": "AE"})
    assert res.status_code == 200
    assert res.json()["tool_calls"][0]["name"] == "audit_statutory_compliance"
    assert res.json()["structured_data"] is None
