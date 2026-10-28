"""Each tool maps to exactly one intent (spec A4)."""

import pytest

from hrms_plugin.agents.supervisor import UserIntent
from hrms_plugin.agents.tools import AgenticToolRegistry
from hrms_plugin.ai.llm import ToolDefinition

EXPECTED = {
    "calculate_salary_structure": UserIntent.PAYROLL_STRUCTURE,
    "calculate_eosb_gratuity": UserIntent.PAYROLL_EOSB,
    "audit_statutory_compliance": UserIntent.COMPLIANCE_AUDIT,
    "search_labor_regulations": UserIntent.LEGAL_QNA,
    "evaluate_leave_request": UserIntent.LEAVE_APPLY,
    "detect_travel_anomaly": UserIntent.ATTENDANCE_ANOMALY,
    "score_candidate_ats": UserIntent.RECRUITMENT_ATS,
    "audit_job_description_bias": UserIntent.RECRUITMENT_BIAS,
    "evaluate_onboarding_offer": UserIntent.ONBOARDING,
    "verify_onboarding_documents": UserIntent.ONBOARDING,
    "audit_offboarding_fnf_timeline": UserIntent.OFFBOARDING_FNF,
    "compute_final_settlement": UserIntent.OFFBOARDING_FNF,
    "audit_travel_expense_claim": UserIntent.TRAVEL_EXPENSE,
    "generate_requisition_archetypes": UserIntent.RECRUITMENT_REQUISITION,
    "calculate_pf_contribution": UserIntent.PAYROLL_STRUCTURE,
}


def test_registered_tools_are_exactly_the_expected_set():
    names = {d.name for d in AgenticToolRegistry().get_definitions()}
    assert names == set(EXPECTED)


@pytest.mark.parametrize("tool,intent", sorted(EXPECTED.items()))
def test_tool_intent(tool, intent):
    assert AgenticToolRegistry().intent_for(tool) == intent


def test_unknown_tool_intent_is_general_hr():
    assert AgenticToolRegistry().intent_for("no_such_tool") == UserIntent.GENERAL_HR


def test_register_without_intent_raises():
    registry = AgenticToolRegistry()
    with pytest.raises(TypeError):
        registry.register(ToolDefinition(name="x", description="x", parameters={}), lambda: {})


def test_get_definition_returns_none_for_unknown():
    assert AgenticToolRegistry().get_definition("no_such_tool") is None
