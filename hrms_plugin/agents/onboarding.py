"""Enterprise Onboarding & Offer Specialist Agent for HRMS Agentic Plugin.

Handles:
1. Pre-hire offer drafting with salary band validation and minimum wage compliance.
2. Mandatory statutory KYC document collection and verification (India PAN/Aadhaar, UAE Emirates ID/ILOE).
3. IT and administrative provisioning plans (accounts, hardware, buddy allocation).
"""

from __future__ import annotations

import re
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from hrms_plugin.rag.store import Jurisdiction


class OfferStatus(str, Enum):
    DRAFTED = "DRAFTED"
    BAND_VIOLATION = "BAND_VIOLATION"
    BELOW_MINIMUM_WAGE = "BELOW_MINIMUM_WAGE"


class OfferEvaluationResult(BaseModel):
    status: OfferStatus
    candidate_name: str
    job_title: str
    annual_salary: Decimal
    currency: str
    within_band: bool
    requires_executive_approval: bool
    explanation: str
    draft_letter: Optional[str] = None
    statutory_citation: Optional[str] = None


class DocumentVerificationStatus(str, Enum):
    VERIFIED = "VERIFIED"
    MISSING_DOCUMENTS = "MISSING_DOCUMENTS"
    INVALID_FORMAT = "INVALID_FORMAT"


class DocumentChecklistResult(BaseModel):
    status: DocumentVerificationStatus
    is_complete: bool
    verified_documents: List[str] = Field(default_factory=list)
    missing_documents: List[str] = Field(default_factory=list)
    invalid_documents: List[Dict[str, str]] = Field(default_factory=list)
    statutory_citations: List[str] = Field(default_factory=list)


class ProvisioningPlanResult(BaseModel):
    employee_id: str
    employee_name: str
    department: str
    it_assets: List[str] = Field(default_factory=list)
    system_accounts: List[str] = Field(default_factory=list)
    buddy_assigned: Optional[str] = None
    first_day_schedule: List[Dict[str, str]] = Field(default_factory=list)


class OnboardingAgent:
    """Specialist agent managing candidate offers, document verification, and provisioning."""

    def evaluate_offer(
        self,
        candidate_name: str,
        job_title: str,
        annual_salary: float | Decimal,
        currency: str = "INR",
        band_min: Optional[float | Decimal] = None,
        band_max: Optional[float | Decimal] = None,
        jurisdiction: Jurisdiction | str = Jurisdiction.INDIA,
    ) -> OfferEvaluationResult:
        """Validate proposed salary against bands and statutory minimums, drafting gated offer."""
        salary = Decimal(str(annual_salary))
        b_min = Decimal(str(band_min)) if band_min is not None else None
        b_max = Decimal(str(band_max)) if band_max is not None else None
        jur = Jurisdiction(jurisdiction) if isinstance(jurisdiction, str) else jurisdiction

        within_band = True
        requires_exec = False
        status = OfferStatus.DRAFTED
        explanation = f"Offer of {currency} {salary:,.2f} is compliant with standard salary bands."
        citation = None

        if b_max and salary > b_max:
            within_band = False
            requires_exec = True
            status = OfferStatus.BAND_VIOLATION
            explanation = (
                f"Proposed salary of {currency} {salary:,.2f} exceeds department band ceiling of "
                f"{currency} {b_max:,.2f} by {currency} {(salary - b_max):,.2f}. Executive approval required."
            )
        elif b_min and salary < b_min:
            within_band = False
            status = OfferStatus.BAND_VIOLATION
            explanation = (
                f"Proposed salary of {currency} {salary:,.2f} falls below department band minimum of "
                f"{currency} {b_min:,.2f}."
            )

        # Statutory Minimum Wage Sanity Check
        if jur == Jurisdiction.UAE:
            # MOHRE skilled worker minimum standard ~ 5,000 AED monthly
            if salary < Decimal("60000"):
                status = OfferStatus.BELOW_MINIMUM_WAGE
                citation = "UAE Federal Decree-Law No. 33 of 2021, Article 27 (Minimum Wage)"
                explanation = f"Salary of AED {salary:,.2f}/yr is below recommended statutory baseline."
        elif jur == Jurisdiction.INDIA:
            citation = "Code on Wages 2019, Section 6 (Floor Wage & Minimum Wages)"

        draft = (
            f"OFFER OF EMPLOYMENT\n\n"
            f"Dear {candidate_name},\n\n"
            f"We are pleased to offer you the position of {job_title}.\n"
            f"Compensation: {currency} {salary:,.2f} per annum.\n"
            f"Subject to background verification and statutory document verification.\n\n"
            f"Status: PROPOSED DRAFT (Requires HR Director sign-off)."
        )

        return OfferEvaluationResult(
            status=status,
            candidate_name=candidate_name,
            job_title=job_title,
            annual_salary=salary,
            currency=currency,
            within_band=within_band,
            requires_executive_approval=requires_exec,
            explanation=explanation,
            draft_letter=draft,
            statutory_citation=citation,
        )

    def verify_onboarding_documents(
        self,
        candidate_name: str,
        jurisdiction: Jurisdiction | str,
        submitted_documents: Dict[str, str],
    ) -> DocumentChecklistResult:
        """Verify statutory KYC documents for identity, social security, and tax enrollment."""
        jur = Jurisdiction(jurisdiction) if isinstance(jurisdiction, str) else jurisdiction
        verified = []
        missing = []
        invalid = []
        citations = []

        if jur == Jurisdiction.INDIA:
            citations.append("Income Tax Act 1961 Section 139A (Mandatory PAN for Tax Withholding)")
            citations.append("Employees' Provident Funds and Miscellaneous Provisions Act, 1952 (UAN Mandatory)")

            # Mandatory: PAN, Aadhaar, Bank Details
            pan = submitted_documents.get("pan")
            if not pan:
                missing.append("PAN Card")
            elif not re.match(r"^[A-Z]{5}[0-9]{4}[A-Z]$", pan.strip().upper()):
                invalid.append(
                    {"document": "PAN Card", "reason": "Invalid format (expected 5 letters, 4 digits, 1 letter)"}
                )
            else:
                verified.append("PAN Card")

            aadhaar = submitted_documents.get("aadhaar")
            if not aadhaar:
                missing.append("Aadhaar Card")
            elif not re.match(r"^\d{12}$", re.sub(r"[\s-]", "", aadhaar)):
                invalid.append({"document": "Aadhaar Card", "reason": "Aadhaar must contain exactly 12 digits"})
            else:
                verified.append("Aadhaar Card")

            if "bank_account" in submitted_documents and "ifsc" in submitted_documents:
                verified.append("Bank Account & IFSC")
            else:
                missing.append("Bank Account Details with IFSC")

        elif jur == Jurisdiction.UAE:
            citations.append("UAE Federal Decree-Law No. 33 of 2021, Article 6 (Work Permits & Residence)")
            citations.append("Cabinet Resolution No. 97 of 2022 (Mandatory ILOE Unemployment Insurance)")

            # Mandatory: Passport, Emirates ID, Visa, ILOE
            eid = submitted_documents.get("emirates_id")
            if not eid:
                missing.append("Emirates ID")
            elif not re.match(r"^784-\d{4}-\d{7}-\d{1}$", eid.strip()) and not re.match(
                r"^784\d{12}$", re.sub(r"[-]", "", eid)
            ):
                invalid.append(
                    {"document": "Emirates ID", "reason": "Invalid Emirates ID format (expected 784-YYYY-XXXXXXX-Z)"}
                )
            else:
                verified.append("Emirates ID")

            passport = submitted_documents.get("passport")
            if not passport:
                missing.append("Passport Copy")
            else:
                verified.append("Passport Copy")

            if submitted_documents.get("iloe_certificate"):
                verified.append("ILOE Unemployment Insurance Certificate")
            else:
                missing.append("ILOE Unemployment Insurance Certificate")

        is_complete = len(missing) == 0 and len(invalid) == 0
        status = (
            DocumentVerificationStatus.VERIFIED
            if is_complete
            else (
                DocumentVerificationStatus.INVALID_FORMAT if invalid else DocumentVerificationStatus.MISSING_DOCUMENTS
            )
        )

        return DocumentChecklistResult(
            status=status,
            is_complete=is_complete,
            verified_documents=verified,
            missing_documents=missing,
            invalid_documents=invalid,
            statutory_citations=citations,
        )

    def generate_provisioning_plan(
        self,
        employee_id: str,
        employee_name: str,
        department: str,
        role: str,
        buddy_name: Optional[str] = None,
    ) -> ProvisioningPlanResult:
        """Generate first-day hardware, IT accounts, and orientation scheduling plan."""
        it_assets = ["Corporate Laptop (Encrypted SSD)", "Security Access Keycard", "Workstation Monitor & Peripherals"]
        if "engineer" in role.lower() or "developer" in role.lower():
            it_assets.append("Hardware 2FA Security Token (YubiKey)")

        system_accounts = [
            f"{employee_id.lower()}@company.internal (Google Workspace / M365)",
            "HRMS Employee Portal Access",
            "Single Sign-On (SSO / Okta)",
            "Slack / Teams Department Workspace",
        ]

        schedule = [
            {"time": "09:30 AM", "activity": "Security Badge Issuance & Laptop Handover"},
            {"time": "10:30 AM", "activity": "HR Welcome & Statutory Document Sign-Off"},
            {"time": "01:00 PM", "activity": f"Welcome Lunch with Onboarding Buddy ({buddy_name or 'Team Lead'})"},
            {"time": "02:30 PM", "activity": "IT Security & SOC2 Compliance Training Walkthrough"},
        ]

        return ProvisioningPlanResult(
            employee_id=employee_id,
            employee_name=employee_name,
            department=department,
            it_assets=it_assets,
            system_accounts=system_accounts,
            buddy_assigned=buddy_name or "Assigned Senior Peer",
            first_day_schedule=schedule,
        )
