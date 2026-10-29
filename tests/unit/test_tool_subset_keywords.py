"""Token waste #3: everyday single words select the right tools; unmatched requests still get every permitted tool.

Extra words feed only tool-subset selection (union of matches, so they can only add tools); classify_intent's
first-match routing for the non-agentic path is unchanged. Whole words only ("pf" never matches inside "pdf").
"""

import json

import pytest

from hrms_plugin.agents.supervisor import SupervisorAgent, UserIntent, matching_intents
from hrms_plugin.agents.tools import AgenticToolRegistry
from hrms_plugin.security.rbac import AuthContext, UserRole

ADMIN = AuthContext(user_id="u", tenant_id="acme", roles=frozenset({UserRole.SUPER_ADMIN}))
REG = AgenticToolRegistry()


@pytest.mark.parametrize("message, intent", [
    ("what is my gratuity", UserIntent.PAYROLL_EOSB),
    ("show my salary", UserIntent.PAYROLL_STRUCTURE),
    ("PF for September", UserIntent.PAYROLL_STRUCTURE),
    ("download my payslip", UserIntent.PAYROLL_STRUCTURE),
    ("I need leave next week", UserIntent.LEAVE_APPLY),
    ("score this resume", UserIntent.RECRUITMENT_ATS),
    ("submit an expense", UserIntent.TRAVEL_EXPENSE),
    ("open a requisition", UserIntent.RECRUITMENT_REQUISITION),
])
def test_everyday_words_match(message, intent):
    assert intent in matching_intents(message)


@pytest.mark.parametrize("message", ["send me the pdf", "please help", "my sleeve is torn"])
def test_no_substring_matches(message):
    assert matching_intents(message) == set()


@pytest.mark.parametrize("message", ["what is my gratuity", "show my salary", "I need leave next week"])
def test_first_match_routing_is_unchanged_by_the_extra_words(message):
    intent, confidence = SupervisorAgent.classify_intent(None, message)
    assert (intent, confidence) == (UserIntent.GENERAL_HR, 0.70)  # the non-agentic path never saw these words


def _schema_chars(names):
    return len(json.dumps([d.parameters for d in REG.get_definitions() if d.name in names]))


def test_every_single_intent_subset_is_far_smaller_than_all_tools():
    all_chars = _schema_chars({d.name for d in REG.definitions_for(ADMIN)})
    for intent in {REG.intent_for(d.name) for d in REG.get_definitions()}:
        names = {d.name for d in REG.get_definitions() if REG.intent_for(d.name) == intent}
        assert _schema_chars(names | {"search_labor_regulations"}) < 0.4 * all_chars, intent
