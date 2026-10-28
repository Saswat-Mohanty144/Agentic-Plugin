"""Enterprise Offboarding & Statutory Final Settlement (FNF) Specialist Agent.

Handles:
1. Statutory 14-day settlement timeline enforcement (UAE Labor Law Art. 53).
2. Comprehensive Gross-to-Net Final Settlement (leave encashment, gratuity, notice buyout).
3. Cross-departmental clearance checklists & experience letter drafting.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field

from hrms_plugin.agents.statutory_payroll import StatutoryPayrollAgent
from hrms_plugin.rag.store import Jurisdiction, StatutoryKnowledgeBase


class FnfDeadlineStatus(str, Enum):
    ON_TRACK = "ON_TRACK"
    APPROACHING_DEADLINE = "APPROACHING_DEADLINE"
    STATUTORY_BREACH = "STATUTORY_BREACH"


class FnfTimelineAudit(BaseModel):
    employee_id: str
    employee_name: str
    last_working_day: str
    days_elapsed: int
    days_remaining: int
    status: FnfDeadlineStatus
    statutory_citation: str
    penalty_risk: Optional[str] = None


class FinalSettlementStatement(BaseModel):
    employee_id: str
    employee_name: str
    jurisdiction: Jurisdiction
    tenure_years: Decimal
    basic_wage: Decimal
    unpaid_salary_days: int
    unpaid_salary_amount: Decimal
    unused_leave_days: Decimal
    leave_encashment_amount: Decimal
    gratuity_amount: Decimal
    notice_adjustment: Decimal  # Positive = company pays, Negative = deduction
    deductions_total: Decimal
    total_net_payable: Decimal
    statutory_citations: List[str] = Field(default_factory=list)


class OffboardingClearance(BaseModel):
    employee_id: str
    employee_name: str
    is_fully_cleared: bool
    pending_items: List[str] = Field(default_factory=list)
    completed_items: List[str] = Field(default_factory=list)
    access_revocation_plan: List[str] = Field(default_factory=list)
    relieving_letter_draft: Optional[str] = None


class OffboardingAgent:
    """Specialist agent auditing and calculating statutory exit settlements and clearances."""

    def __init__(
        self,
        kb: Optional[StatutoryKnowledgeBase] = None,
        payroll_agent: Optional[StatutoryPayrollAgent] = None,
    ):
        self.kb = kb or StatutoryKnowledgeBase()
        self.payroll_agent = payroll_agent or StatutoryPayrollAgent(kb=self.kb)

    def audit_uae_settlement_timeline(
        self,
        employee_id: str,
        employee_name: str,
        last_working_day: str,
        current_date_str: Optional[str] = None,
    ) -> FnfTimelineAudit:
        """Enforce UAE Federal Decree-Law No. 33 of 2021 Article 53 (14-day payment mandate)."""
        lwd = date.fromisoformat(last_working_day)
        today = date.fromisoformat(current_date_str) if current_date_str else date.today()
        elapsed = (today - lwd).days
        remaining = 14 - elapsed

        citation = "UAE Federal Decree-Law No. 33 of 2021, Article 53 (14-Day Final Settlement Mandate)"

        if remaining < 0:
            status = FnfDeadlineStatus.STATUTORY_BREACH
            penalty = (
                f"Settlement is overdue by {abs(remaining)} days. Risk of MOHRE labor grievance, "
                "financial penalty, and company work permit blocking."
            )
        elif remaining <= 3:
            status = FnfDeadlineStatus.APPROACHING_DEADLINE
            penalty = f"Critical settlement deadline in {remaining} days. Urgent payroll release required."
        else:
            status = FnfDeadlineStatus.ON_TRACK
            penalty = None

        return FnfTimelineAudit(
            employee_id=employee_id,
            employee_name=employee_name,
            last_working_day=last_working_day,
            days_elapsed=elapsed,
            days_remaining=max(0, remaining),
            status=status,
            statutory_citation=citation,
            penalty_risk=penalty,
        )

    def compute_final_settlement(
        self,
        employee_id: str,
        employee_name: str,
        basic_wage_monthly: float | Decimal,
        gross_wage_monthly: float | Decimal,
        tenure_years: float | Decimal,
        unused_leave_days: float | Decimal = 0.0,
        unpaid_work_days: int = 0,
        notice_days_to_pay: int = 0,
        notice_days_to_deduct: int = 0,
        jurisdiction: Jurisdiction | str = Jurisdiction.UAE,
    ) -> FinalSettlementStatement:
        """Calculate complete final settlement statement with decimal exactness."""
        jur = Jurisdiction(jurisdiction) if isinstance(jurisdiction, str) else jurisdiction
        basic = Decimal(str(basic_wage_monthly))
        gross = Decimal(str(gross_wage_monthly))
        tenure = Decimal(str(tenure_years))
        leave_days = Decimal(str(unused_leave_days))

        citations: List[str] = []

        # 1. Partial Month Salary
        daily_gross = (gross / Decimal("30")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        unpaid_salary = daily_gross * Decimal(str(unpaid_work_days))

        # 2. Leave Encashment (Calculated on basic wage / 30)
        daily_basic = (basic / Decimal("30")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        leave_encashment = (daily_basic * leave_days).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        # 3. Gratuity / End of Service Severance
        if jur == Jurisdiction.UAE:
            citations.append("UAE Federal Decree-Law No. 33 of 2021, Article 51 (End of Service Gratuity)")
            citations.append("UAE Federal Decree-Law No. 33 of 2021, Article 53 (Payment Timeframe)")
            eosb_res = self.payroll_agent.calculate_uae_eosb(
                basic_wage_monthly=float(basic),
                tenure_years=float(tenure),
            )
            gratuity = Decimal(str(eosb_res.total_eosb_gratuity))
        else:
            citations.append("Payment of Gratuity Act, 1972 Section 4 (India 15/26 Formula)")
            grat_res = self.payroll_agent.calculate_india_gratuity(
                last_drawn_basic=float(basic),
                tenure_years=float(tenure),
            )
            gratuity = Decimal(str(grat_res.gratuity_amount))

        # 4. Notice Adjustments
        notice_pay = daily_gross * Decimal(str(notice_days_to_pay))
        notice_deduct = daily_gross * Decimal(str(notice_days_to_deduct))
        notice_adjustment = notice_pay - notice_deduct

        # 5. Net Payable
        net_payable = (unpaid_salary + leave_encashment + gratuity + notice_adjustment).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

        return FinalSettlementStatement(
            employee_id=employee_id,
            employee_name=employee_name,
            jurisdiction=jur,
            tenure_years=tenure,
            basic_wage=basic,
            unpaid_salary_days=unpaid_work_days,
            unpaid_salary_amount=unpaid_salary,
            unused_leave_days=leave_days,
            leave_encashment_amount=leave_encashment,
            gratuity_amount=gratuity,
            notice_adjustment=notice_adjustment,
            deductions_total=notice_deduct,
            total_net_payable=net_payable,
            statutory_citations=citations,
        )

    def evaluate_exit_clearance(
        self,
        employee_id: str,
        employee_name: str,
        department: str,
        returned_assets: List[str],
        pending_assets: List[str],
        finance_cleared: bool = True,
    ) -> OffboardingClearance:
        """Evaluate exit clearance across IT, Finance, and HR operations."""
        pending = list(pending_assets)
        if not finance_cleared:
            pending.append("Outstanding corporate expense or travel advance reconciliation")

        completed = list(returned_assets)
        if finance_cleared:
            completed.append("Finance & expense reconciliation verified")

        is_cleared = len(pending) == 0

        revocation = [
            f"Disable Active Directory / Okta SSO Account ({employee_id})",
            "Revoke corporate email forwarding and OAuth sessions",
            "De-provision department Slack / Teams channels",
            "Revoke physical office access card and parking permits",
        ]

        relieving_draft = None
        if is_cleared:
            relieving_draft = (
                f"EXPERIENCE & RELIEVING CERTIFICATE\n\n"
                f"To Whom It May Concern,\n\n"
                f"This is to certify that {employee_name} (Employee ID: {employee_id}) was employed with us "
                f"in the {department} department. All organizational dues and property have been satisfactorily "
                f"cleared. We wish them success in their future endeavors.\n\n"
                f"Authorized Signatory, HR Operations."
            )

        return OffboardingClearance(
            employee_id=employee_id,
            employee_name=employee_name,
            is_fully_cleared=is_cleared,
            pending_items=pending,
            completed_items=completed,
            access_revocation_plan=revocation,
            relieving_letter_draft=relieving_draft,
        )
