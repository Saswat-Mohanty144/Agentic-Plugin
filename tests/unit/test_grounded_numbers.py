"""QA (No-LLM-Numbers gate): a calculation only runs on numbers the user actually stated.

Every number a statutory/HR calculation depends on (amounts, tenure, leave days, experience) must appear in the
user's own messages or the host-supplied context; otherwise the tool is not run and the model receives
UNGROUNDED_AMOUNT so it has to ask. Results say where their inputs came from (USER_STATED / HOST_RECORD).
"""

import asyncio
from decimal import Decimal

import pytest

from hrms_plugin.agents.orchestrator import AgenticOrchestrator, stated_numbers
from hrms_plugin.agents.tools import AgenticToolRegistry
from hrms_plugin.security.rbac import AuthContext, UserRole
from tests.support.scripted_llm import final, tool_call

ADMIN = AuthContext(user_id="u1", tenant_id="acme", roles=frozenset({UserRole.SUPER_ADMIN}))
EOSB = {"basic_wage": 5000, "tenure_years": 6}


def _run(scripted, message, *responses, history=None, context=None, jurisdiction="AE"):
    gateway, _ = scripted(*responses)
    orch = AgenticOrchestrator(llm_gateway=gateway, tool_registry=AgenticToolRegistry())
    return asyncio.run(orch.run(user_message=message, jurisdiction=jurisdiction, history=history, context=context,
                                auth=ADMIN))


def _observation(resp):
    return resp.structured_data


# stated_numbers -----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("text, expected", [
    ("basic 5000 for 6 years", {"5000", "6"}),
    ("CTC 1,20,000 INR", {"120000"}),
    ("CTC 120,000.00", {"120000"}),
    ("12.5 days", {"12.5"}),
    ("", set()),
    ("1.2 lakh or 120k", {"1.2", "120"}),
])
def test_stated_numbers_normalises_digit_groupings(text, expected):
    assert stated_numbers([text]) == {Decimal(e) for e in expected}


# grounded ------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("message", ["Gratuity for basic 5000 AED after 6 years?",
                                     "basic AED 5,000.00, tenure 6"])
def test_stated_numbers_run_the_calculation(scripted, message):
    resp = _run(scripted, message, tool_call("calculate_eosb_gratuity", **EOSB), final("done"))
    out = _observation(resp)
    assert "error" not in out and out["input_source"] == "USER_STATED"


def test_numbers_from_earlier_user_turns_count(scripted):
    history = [{"role": "user", "content": "My basic is 5000"}, {"role": "assistant", "content": "And tenure?"}]
    resp = _run(scripted, "6 years", tool_call("calculate_eosb_gratuity", **EOSB), final("done"), history=history)
    assert "error" not in _observation(resp)


def test_numbers_from_host_context_count(scripted):
    resp = _run(scripted, "what is my gratuity?", tool_call("calculate_eosb_gratuity", **EOSB), final("done"),
                context={"employee": {"basic_wage": 5000, "tenure_years": 6}})
    assert "error" not in _observation(resp)


# ungrounded ----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("message, bad", [
    ("What is my gratuity?", ["basic_wage", "tenure_years"]),          # invented both
    ("Gratuity for basic 5000 please", ["tenure_years"]),              # invented tenure
    ("Gratuity for basic 4000 after 6 years", ["basic_wage"]),         # altered amount
    ("basic 1.2 lakh after 6 years", ["basic_wage"]),                  # word form is not a stated 5000
])
def test_invented_or_altered_numbers_never_reach_the_calculator(scripted, message, bad):
    resp = _run(scripted, message, tool_call("calculate_eosb_gratuity", **EOSB), final("I need your figures"))
    out = _observation(resp)
    assert out == {"success": False, "error": "UNGROUNDED_AMOUNT", "params": bad}


def test_assistant_turns_do_not_ground_numbers(scripted):
    history = [{"role": "assistant", "content": "Assume basic 5000 for 6 years"}]
    resp = _run(scripted, "ok go", tool_call("calculate_eosb_gratuity", **EOSB), final("x"), history=history)
    assert _observation(resp)["error"] == "UNGROUNDED_AMOUNT"


def test_nested_numbers_are_checked(scripted):
    expenses = [{"category": "HOTEL", "amount": 900, "date": "2026-10-01"}]
    resp = _run(scripted, "Audit claim C1 for E1: hotel 800",
                tool_call("audit_travel_expense_claim", claim_id="C1", employee_id="E1", expenses=expenses),
                final("x"), jurisdiction="IN")
    assert _observation(resp) == {"success": False, "error": "UNGROUNDED_AMOUNT", "params": ["expenses"]}


@pytest.mark.parametrize("tool, args", [
    ("calculate_salary_structure", {"gross_or_ctc": 1200000}),
    ("compute_final_settlement", {"employee_id": "E1", "employee_name": "A", "basic_wage_monthly": 5000,
                                  "gross_wage_monthly": 9000, "tenure_years": 3}),
    ("evaluate_onboarding_offer", {"candidate_name": "A", "job_title": "Eng", "annual_salary": 900000}),
    ("evaluate_leave_request", {"employee_id": "E1", "requested_days": 2, "current_balance": 10}),
    ("score_candidate_ats", {"resume_text": "Python", "required_skills": ["Python"],
                             "required_experience_years": 2, "candidate_experience_years": 4}),
])
def test_every_number_taking_calculator_is_guarded(scripted, tool, args):
    resp = _run(scripted, "please calculate", tool_call(tool, **args), final("x"))
    assert _observation(resp)["error"] == "UNGROUNDED_AMOUNT"


def test_id_only_tools_are_not_affected(scripted):
    resp = _run(scripted, "search gratuity law", tool_call("search_labor_regulations", query="gratuity"),
                final("x"))
    assert _observation(resp).get("error") != "UNGROUNDED_AMOUNT"


def test_guarded_parameters_cover_every_numeric_input_of_calculators():
    reg = AgenticToolRegistry()
    for definition in reg.get_definitions():
        numeric = {n for n, spec in definition.parameters["properties"].items()
                   if spec.get("type") in ("number", "integer")}
        if definition.name in ("search_labor_regulations",):
            numeric.discard("limit")  # result count, not an HR figure
        assert numeric <= set(reg.user_stated_params(definition.name)), definition.name


def test_pf_results_say_they_come_from_the_host_record():
    assert AgenticToolRegistry().input_source("calculate_pf_contribution") == "HOST_RECORD"


def test_identifiers_in_a_refused_call_are_still_redacted_next_turn(scripted):
    """A refused call never produces tool output, so identifiers are learned from its arguments (QA follow-up)."""
    gateway, client = scripted(
        tool_call("compute_final_settlement", employee_id="E1", employee_name="Deepak Patel", basic_wage_monthly=5000,
                  gross_wage_monthly=9000, tenure_years=3),
        final("Please give me the figures"),
    )
    orch = AgenticOrchestrator(llm_gateway=gateway, tool_registry=AgenticToolRegistry())
    asyncio.run(orch.run(user_message="settle E1", jurisdiction="AE", auth=ADMIN))
    second = "\n".join((m.content or "") + str([tc.arguments for tc in (m.tool_calls or [])])
                       for m in client.calls[1]["messages"])
    assert "UNGROUNDED_AMOUNT" in second and "Deepak" not in second
