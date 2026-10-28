"""Unit tests for Onboarding & Offer Specialist Agent."""

from decimal import Decimal

import pytest

from hrms_plugin.agents.onboarding import (
    DocumentVerificationStatus,
    OfferStatus,
    OnboardingAgent,
)
from hrms_plugin.rag.store import Jurisdiction


@pytest.fixture
def onboarding_agent():
    return OnboardingAgent()


def test_evaluate_offer_within_band(onboarding_agent):
    """Test standard offer within department salary band."""
    res = onboarding_agent.evaluate_offer(
        candidate_name="Alice Kumar",
        job_title="Senior Backend Engineer",
        annual_salary=1800000.0,
        currency="INR",
        band_min=1400000.0,
        band_max=2200000.0,
        jurisdiction=Jurisdiction.INDIA,
    )

    assert res.status == OfferStatus.DRAFTED
    assert res.within_band is True
    assert res.requires_executive_approval is False
    assert res.annual_salary == Decimal("1800000.0")
    assert "Alice Kumar" in res.draft_letter
    assert "Code on Wages" in res.statutory_citation


def test_evaluate_offer_exceeds_band(onboarding_agent):
    """Test offer that exceeds salary ceiling requiring executive sign-off."""
    res = onboarding_agent.evaluate_offer(
        candidate_name="Bob Martin",
        job_title="Staff AI Architect",
        annual_salary=450000.0,
        currency="AED",
        band_min=200000.0,
        band_max=350000.0,
        jurisdiction=Jurisdiction.UAE,
    )

    assert res.status == OfferStatus.BAND_VIOLATION
    assert res.within_band is False
    assert res.requires_executive_approval is True
    assert "exceeds department band ceiling" in res.explanation


def test_verify_kyc_documents_india(onboarding_agent):
    """Test mandatory KYC verification for India (PAN + Aadhaar + Bank)."""
    # 1. Complete and valid
    valid_docs = {
        "pan": "ABCDE1234F",
        "aadhaar": "1234 5678 9012",
        "bank_account": "9876543210",
        "ifsc": "HDFC0001234",
    }
    res_valid = onboarding_agent.verify_onboarding_documents(
        candidate_name="Charlie Roy",
        jurisdiction=Jurisdiction.INDIA,
        submitted_documents=valid_docs,
    )
    assert res_valid.is_complete is True
    assert res_valid.status == DocumentVerificationStatus.VERIFIED
    assert len(res_valid.missing_documents) == 0
    assert len(res_valid.invalid_documents) == 0

    # 2. Invalid PAN format & missing Aadhaar
    invalid_docs = {
        "pan": "INVALID_PAN_123",
    }
    res_inv = onboarding_agent.verify_onboarding_documents(
        candidate_name="Charlie Roy",
        jurisdiction=Jurisdiction.INDIA,
        submitted_documents=invalid_docs,
    )
    assert res_inv.is_complete is False
    assert res_inv.status == DocumentVerificationStatus.INVALID_FORMAT
    assert any(d["document"] == "PAN Card" for d in res_inv.invalid_documents)
    assert "Aadhaar Card" in res_inv.missing_documents


def test_verify_kyc_documents_uae(onboarding_agent):
    """Test mandatory KYC verification for UAE (Emirates ID + Passport + ILOE)."""
    valid_docs = {
        "emirates_id": "784-1990-1234567-1",
        "passport": "P12345678",
        "iloe_certificate": "ILOE-CERT-999",
    }
    res = onboarding_agent.verify_onboarding_documents(
        candidate_name="Fatima Al Mansoori",
        jurisdiction=Jurisdiction.UAE,
        submitted_documents=valid_docs,
    )
    assert res.is_complete is True
    assert res.status == DocumentVerificationStatus.VERIFIED
    assert "Emirates ID" in res.verified_documents
    assert any("Cabinet Resolution No. 97" in c for c in res.statutory_citations)


def test_generate_provisioning_plan(onboarding_agent):
    """Test first-day IT and orientation plan creation."""
    plan = onboarding_agent.generate_provisioning_plan(
        employee_id="EMP-900",
        employee_name="David Chen",
        department="Engineering",
        role="Senior Lead Developer",
        buddy_name="Sarah Connor",
    )
    assert plan.employee_id == "EMP-900"
    assert "Hardware 2FA Security Token (YubiKey)" in plan.it_assets
    assert any("Slack" in a for a in plan.system_accounts)
    assert plan.buddy_assigned == "Sarah Connor"
    assert len(plan.first_day_schedule) >= 3
