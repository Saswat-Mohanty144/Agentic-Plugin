"""Unit tests for Offboarding & Statutory Final Settlement (FNF) Specialist Agent."""

from decimal import Decimal

import pytest

from hrms_plugin.agents.offboarding import FnfDeadlineStatus, OffboardingAgent
from hrms_plugin.rag.store import Jurisdiction


@pytest.fixture
def offboarding_agent():
    return OffboardingAgent()


def test_audit_uae_settlement_timeline_on_track(offboarding_agent):
    """Test UAE Art. 53 audit when within 14-day statutory timeline."""
    audit = offboarding_agent.audit_uae_settlement_timeline(
        employee_id="EMP-100",
        employee_name="Zaid Khan",
        last_working_day="2026-09-20",
        current_date_str="2026-09-25",  # 5 days elapsed, 9 remaining
    )
    assert audit.status == FnfDeadlineStatus.ON_TRACK
    assert audit.days_elapsed == 5
    assert audit.days_remaining == 9
    assert "Article 53" in audit.statutory_citation


def test_audit_uae_settlement_timeline_breach(offboarding_agent):
    """Test UAE Art. 53 audit when 14-day statutory deadline has passed."""
    audit = offboarding_agent.audit_uae_settlement_timeline(
        employee_id="EMP-100",
        employee_name="Zaid Khan",
        last_working_day="2026-09-01",
        current_date_str="2026-09-25",  # 24 days elapsed, -10 remaining
    )
    assert audit.status == FnfDeadlineStatus.STATUTORY_BREACH
    assert audit.days_remaining == 0
    assert "Risk of MOHRE labor grievance" in audit.penalty_risk


def test_compute_final_settlement_uae(offboarding_agent):
    """Test decimal-exact UAE final settlement (salary + leave encashment + gratuity)."""
    # basic: 15,000 AED, gross: 25,000 AED, tenure: 3 years
    # unpaid: 6 days, leave: 10 days
    # daily gross = 25000 / 30 = 833.33 * 6 = 5000.00 (approx/exact rounded)
    # daily basic = 15000 / 30 = 500.00 * 10 = 5000.00
    # gratuity (3 yrs @ 21 days/yr) = 63 days * (15000/30 = 500) = 31,500.00
    statement = offboarding_agent.compute_final_settlement(
        employee_id="EMP-UAE-77",
        employee_name="Rashid Ahmed",
        basic_wage_monthly=15000.0,
        gross_wage_monthly=25000.0,
        tenure_years=3.0,
        unused_leave_days=10.0,
        unpaid_work_days=6,
        jurisdiction=Jurisdiction.UAE,
    )

    assert statement.employee_id == "EMP-UAE-77"
    assert statement.gratuity_amount == Decimal("31500.00")
    assert statement.leave_encashment_amount == Decimal("5000.00")
    assert statement.total_net_payable > Decimal("40000.00")
    assert any("Article 51" in c for c in statement.statutory_citations)


def test_compute_final_settlement_india(offboarding_agent):
    """Test decimal-exact India final settlement with notice buyout adjustment."""
    # basic: 50,000 INR, gross: 100,000 INR, tenure: 6 years
    # gratuity: (15 * 50,000 * 6) / 26 = 173,076.92
    statement = offboarding_agent.compute_final_settlement(
        employee_id="EMP-IN-55",
        employee_name="Rajesh Sharma",
        basic_wage_monthly=50000.0,
        gross_wage_monthly=100000.0,
        tenure_years=6.0,
        unused_leave_days=15.0,
        unpaid_work_days=10,
        notice_days_to_pay=0,
        notice_days_to_deduct=5,  # Deduct 5 days notice
        jurisdiction=Jurisdiction.INDIA,
    )

    assert statement.jurisdiction == Jurisdiction.INDIA
    assert statement.gratuity_amount == Decimal("173076.92")
    assert statement.deductions_total > Decimal("0.00")
    assert statement.total_net_payable > Decimal("100000.00")


def test_evaluate_exit_clearance(offboarding_agent):
    """Test exit clearance evaluation and experience letter generation."""
    # 1. Pending IT assets
    res_pending = offboarding_agent.evaluate_exit_clearance(
        employee_id="EMP-01",
        employee_name="Sara Miller",
        department="Product",
        returned_assets=["Office Keycard"],
        pending_assets=["MacBook Pro 16-inch"],
        finance_cleared=True,
    )
    assert res_pending.is_fully_cleared is False
    assert res_pending.relieving_letter_draft is None

    # 2. Fully cleared
    res_cleared = offboarding_agent.evaluate_exit_clearance(
        employee_id="EMP-01",
        employee_name="Sara Miller",
        department="Product",
        returned_assets=["Office Keycard", "MacBook Pro 16-inch"],
        pending_assets=[],
        finance_cleared=True,
    )
    assert res_cleared.is_fully_cleared is True
    assert res_cleared.relieving_letter_draft is not None
    assert "EXPERIENCE & RELIEVING CERTIFICATE" in res_cleared.relieving_letter_draft
