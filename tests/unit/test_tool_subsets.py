"""QE-2 (Q4): the model is offered only the tools matching the request's intents, never fewer than it needs.

Deterministic keyword intents (the same lists as SupervisorAgent.classify_intent) select tools; the regulation search
is always offered; anything unmatched, or a match the user has no permitted tool for, falls back to every permitted
tool. Permissions are never widened.
"""

import asyncio

import pytest

from hrms_plugin.agents.orchestrator import AgenticOrchestrator
from hrms_plugin.agents.supervisor import SupervisorAgent, UserIntent, matching_intents
from hrms_plugin.agents.tools import AgenticToolRegistry
from hrms_plugin.security.rbac import AuthContext, UserRole
from tests.support.scripted_llm import final

ADMIN = AuthContext(user_id="u1", tenant_id="acme", roles=frozenset({UserRole.SUPER_ADMIN}))
STAFF = AuthContext(user_id="u2", tenant_id="acme", roles=frozenset({UserRole.EMPLOYEE}))
ALL = sorted(d.name for d in AgenticToolRegistry().definitions_for(ADMIN))


def offered(scripted, message, auth=ADMIN, history=None):
    gateway, client = scripted(final("ok"))
    orch = AgenticOrchestrator(llm_gateway=gateway, tool_registry=AgenticToolRegistry())
    asyncio.run(orch.run(user_message=message, jurisdiction="AE", history=history, auth=auth))
    return sorted(t.name for t in client.calls[0]["tools"] or [])


@pytest.mark.parametrize("message, intents", [
    ("calculate eosb please", {UserIntent.PAYROLL_EOSB}),
    ("eosb and a compliance check", {UserIntent.PAYROLL_EOSB, UserIntent.COMPLIANCE_AUDIT}),
    ("hello there", set()),
])
def test_matching_intents_returns_every_match(message, intents):
    assert matching_intents(message) == intents


@pytest.mark.parametrize("message", ["calculate eosb please", "salary structure for 1200000", "hello there",
                                     "audit job description for bias"])
def test_classify_intent_is_unchanged(message):
    """The refactor keeps the first-match behaviour of the non-agentic path."""
    intent, _ = SupervisorAgent.classify_intent(None, message)
    matches = matching_intents(message)
    assert (intent in matches) if matches else intent is UserIntent.GENERAL_HR


def test_single_intent_offers_its_tools_plus_regulation_search(scripted):
    assert offered(scripted, "calculate eosb for basic 5000") == ["calculate_eosb_gratuity",
                                                                  "search_labor_regulations"]


def test_multiple_intents_offer_the_union(scripted):
    tools = offered(scripted, "eosb and a compliance check")
    assert tools == ["audit_statutory_compliance", "calculate_eosb_gratuity", "search_labor_regulations"]


def test_unmatched_message_offers_every_permitted_tool(scripted):
    assert offered(scripted, "hello there") == ALL


def test_earlier_user_turns_keep_their_tools(scripted):
    history = [{"role": "user", "content": "calculate eosb"}, {"role": "assistant", "content": "Tenure?"}]
    assert "calculate_eosb_gratuity" in offered(scripted, "6 years", history=history)


def test_subsets_never_widen_permissions(scripted):
    staff_all = sorted(d.name for d in AgenticToolRegistry().definitions_for(STAFF))
    tools = offered(scripted, "calculate eosb please", auth=STAFF)  # staff may not see salary tools
    assert set(tools) <= set(staff_all) and "calculate_eosb_gratuity" not in tools
    assert tools == staff_all  # nothing permitted matched -> every permitted tool, not an empty list


def test_subset_is_smaller_than_the_full_list(scripted):
    assert len(offered(scripted, "calculate eosb please")) < len(ALL) / 3
