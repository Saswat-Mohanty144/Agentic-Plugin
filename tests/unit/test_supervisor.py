"""Unit tests for Multi-Agent Supervisor."""

import pytest
from hrms_plugin.agents.supervisor import SupervisorAgent, UserIntent
from hrms_plugin.rag.store import Jurisdiction


@pytest.fixture
def supervisor():
    return SupervisorAgent()


def test_intent_classification(supervisor):
    """Test intent classification accuracy across domain specialist queries."""
    intents = [
        ("Calculate take home and salary structure for CTC 15 lakhs", UserIntent.PAYROLL_STRUCTURE),
        ("Calculate UAE end of service gratuity for 4 years", UserIntent.PAYROLL_EOSB),
        ("Run statutory compliance audit for employees", UserIntent.COMPLIANCE_AUDIT),
        ("Apply leave for 3 days annual leave", UserIntent.LEAVE_APPLY),
        ("Check impossible travel punch anomaly", UserIntent.ATTENDANCE_ANOMALY),
        ("Match candidate resume and give ats score", UserIntent.RECRUITMENT_ATS),
        ("Audit job description for gender bias", UserIntent.RECRUITMENT_BIAS),
        ("What is the labor code section for overtime rule in UAE?", UserIntent.LEGAL_QNA),
    ]

    for query, expected_intent in intents:
        intent, conf = supervisor.classify_intent(query)
        assert intent == expected_intent, f"Failed for query: {query}. Got: {intent}, expected: {expected_intent}"
        assert conf >= 0.85


def test_process_payroll_message(supervisor):
    """Test full supervisor routing for India salary structure."""
    response = supervisor.process_message(
        message="Calculate CTC breakdown and net take home",
        jurisdiction=Jurisdiction.INDIA,
        context={"annual_ctc": 1200000.0},
    )

    assert response.intent == UserIntent.PAYROLL_STRUCTURE
    assert response.routed_agent == "StatutoryPayrollAgent"
    assert response.structured_data is not None
    assert response.structured_data["net_take_home"] == 88276.92
    assert len(response.statutory_citations) > 0


def test_process_eosb_message(supervisor):
    """Test full supervisor routing for UAE EOSB."""
    response = supervisor.process_message(
        message="Calculate UAE end of service severance gratuity",
        jurisdiction=Jurisdiction.UAE,
        context={"basic_wage_monthly": 10000.0, "tenure_years": 4.0},
    )

    assert response.intent == UserIntent.PAYROLL_EOSB
    assert response.routed_agent == "StatutoryPayrollAgent"
    # 4 yrs * 21 days * (10000/30 = 333.33) = 28,000 AED
    assert response.structured_data["total_eosb_gratuity"] == 28000.0
    assert "Article 51" in response.statutory_citations[0]
