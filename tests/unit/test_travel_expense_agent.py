"""Unit tests for Travel & Expense (T&E) Claim Audit Specialist Agent."""

from decimal import Decimal

import pytest

from hrms_plugin.agents.travel_expense import (
    ExpenseViolationSeverity,
    TravelExpenseAgent,
)


@pytest.fixture
def travel_agent():
    return TravelExpenseAgent()


def test_audit_claim_compliant(travel_agent):
    """Test claim that meets all per-diem and documentation rules."""
    expenses = [
        {"id": "EXP-1", "category": "MEAL", "amount": "1200.00", "has_receipt": True, "receipt_uri": "receipts/m1.pdf"},
        {
            "id": "EXP-2",
            "category": "LOCAL_CONVEYANCE",
            "amount": "800.00",
            "has_receipt": True,
            "receipt_uri": "receipts/c1.pdf",
        },
    ]
    report = travel_agent.audit_claim(
        claim_id="CLM-001",
        employee_id="EMP-TRV-1",
        expenses=expenses,
        currency="INR",
        tier="METRO",
        has_approved_travel_request=True,
    )

    assert report.is_fully_approved is True
    assert report.total_claimed == Decimal("2000.00")
    assert report.total_approved == Decimal("2000.00")
    assert report.total_disallowed == Decimal("0.00")
    assert len(report.violations) == 0


def test_audit_claim_exceeds_per_diem(travel_agent):
    """Test meal claim that exceeds daily per-diem allowance."""
    # Metro INR cap is 3000.00
    expenses = [
        {
            "id": "EXP-10",
            "category": "MEAL",
            "amount": "4500.00",
            "has_receipt": True,
            "receipt_uri": "receipts/m2.pdf",
        },
    ]
    report = travel_agent.audit_claim(
        claim_id="CLM-002",
        employee_id="EMP-TRV-2",
        expenses=expenses,
        currency="INR",
        tier="METRO",
        has_approved_travel_request=True,
    )

    assert report.is_fully_approved is False
    assert report.total_claimed == Decimal("4500.00")
    assert report.total_approved == Decimal("3000.00")
    assert report.total_disallowed == Decimal("1500.00")
    assert len(report.violations) == 1
    assert report.violations[0].severity == ExpenseViolationSeverity.EXCEEDS_PER_DIEM


def test_audit_claim_unauthorized_flight(travel_agent):
    """Test flight expense submitted without pre-approved corporate travel request."""
    expenses = [
        {"id": "EXP-99", "category": "FLIGHT", "amount": "12000.00", "has_receipt": True},
    ]
    report = travel_agent.audit_claim(
        claim_id="CLM-003",
        employee_id="EMP-TRV-3",
        expenses=expenses,
        currency="INR",
        tier="METRO",
        has_approved_travel_request=False,
    )

    assert report.is_fully_approved is False
    assert report.total_disallowed == Decimal("12000.00")
    assert report.total_approved == Decimal("0.00")
    assert report.violations[0].severity == ExpenseViolationSeverity.POLICY_BREACH


def test_audit_claim_missing_receipt_and_duplicate(travel_agent):
    """Test detection of missing receipt and duplicate receipt submissions."""
    expenses = [
        {"id": "EXP-1", "category": "HOTEL", "amount": "5000.00", "receipt_uri": "receipts/hotel_01.pdf"},
        {"id": "EXP-2", "category": "HOTEL", "amount": "5000.00", "receipt_uri": "receipts/hotel_01.pdf"},  # Duplicate
        {"id": "EXP-3", "category": "MEAL", "amount": "500.00", "has_receipt": False},  # Missing
    ]
    report = travel_agent.audit_claim(
        claim_id="CLM-004",
        employee_id="EMP-TRV-4",
        expenses=expenses,
        currency="INR",
        tier="METRO",
        has_approved_travel_request=True,
    )

    severities = [v.severity for v in report.violations]
    assert ExpenseViolationSeverity.SUSPECTED_DUPLICATE in severities
    assert ExpenseViolationSeverity.MISSING_DOCUMENTATION in severities
