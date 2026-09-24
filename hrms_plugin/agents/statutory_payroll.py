"""Statutory Payroll Specialist Agent: Decimal-Exact Tax & CTC Structuring (India & UAE)."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from hrms_plugin.rag.store import Jurisdiction, StatutoryKnowledgeBase


def _d(val: float | int | str | Decimal) -> Decimal:
    """Helper to convert numeric inputs into exact 2-decimal rounded Decimals."""
    if isinstance(val, Decimal):
        return val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class IndiaSalaryStructure(BaseModel):
    annual_ctc: float
    monthly_ctc: float
    basic_salary: float
    hra: float
    special_allowance: float
    gross_salary: float
    employee_pf: float
    employer_epf: float
    employer_eps: float
    employer_pf_total: float
    employee_esi: float
    employer_esi: float
    professional_tax: float
    gratuity_monthly_provision: float
    net_take_home: float
    statutory_citations: List[str] = Field(default_factory=list)


class UaeSalaryStructure(BaseModel):
    monthly_gross: float
    basic_wage: float
    housing_allowance: float
    transport_allowance: float
    other_allowances: float
    wps_compliant: bool
    estimated_monthly_eosb_accrual: float
    statutory_citations: List[str] = Field(default_factory=list)


class UaeEosbCalculation(BaseModel):
    tenure_years: float
    basic_wage_monthly: float
    total_eosb_gratuity: float
    formula_breakdown: str
    statutory_citation: str


class StatutoryPayrollAgent:
    """Specialist agent responsible for mathematical accuracy and statutory payroll structuring."""

    def __init__(self, kb: Optional[StatutoryKnowledgeBase] = None):
        self.kb = kb or StatutoryKnowledgeBase()

    def structure_india_salary(
        self,
        annual_ctc: float | Decimal,
        basic_percent: float = 40.0,
        hra_percent: float = 20.0,
        cap_pf_at_ceiling: bool = False,
        pt_amount: float = 200.0,
    ) -> IndiaSalaryStructure:
        """Compute exact gross-to-net salary breakdown under Indian labor statutes.

        All arithmetic is performed with Python Decimal to prevent floating point penny drift.
        """
        ann_ctc = _d(annual_ctc)
        mon_ctc = _d(ann_ctc / Decimal("12.0"))

        # Basic Salary
        basic = _d(mon_ctc * (_d(basic_percent) / Decimal("100.0")))
        
        # HRA (typically 50% of basic or 20% of CTC)
        hra = _d(mon_ctc * (_d(hra_percent) / Decimal("100.0")))

        # EPF Calculations
        # EPF wage ceiling is INR 15,000
        pf_wage_base = min(basic, Decimal("15000.00")) if cap_pf_at_ceiling else basic
        
        # Employee PF: 12% of basic
        emp_pf = _d(pf_wage_base * Decimal("0.12"))

        # Employer PF: 12% total -> 8.33% to EPS (capped at 15000 ceiling = 1250) + remainder to EPF
        eps_base = min(pf_wage_base, Decimal("15000.00"))
        employer_eps = _d(eps_base * (_d("8.33") / Decimal("100.0")))
        # Capping EPS at maximum 1250 INR per month statutory limit
        if employer_eps > Decimal("1250.00"):
            employer_eps = Decimal("1250.00")
            
        employer_pf_total = _d(pf_wage_base * Decimal("0.12"))
        employer_epf = _d(employer_pf_total - employer_eps)

        # Gratuity Monthly Provision (15/26 days per year = 4.81% of basic)
        gratuity_prov = _d(basic * (Decimal("15") / (Decimal("26") * Decimal("12"))))

        # Gross Salary = Monthly CTC - Employer Contributions (PF + Gratuity Provision)
        gross = _d(mon_ctc - employer_pf_total - gratuity_prov)

        # Special Allowance is the balancing component
        special_allowance = _d(gross - basic - hra)
        if special_allowance < Decimal("0.00"):
            # Rebalance if gross is smaller than basic + hra
            special_allowance = Decimal("0.00")
            gross = _d(basic + hra)

        # ESI Calculations (Gross <= 21,000 INR)
        if gross <= Decimal("21000.00"):
            emp_esi = _d(gross * (_d("0.75") / Decimal("100.0")))
            empr_esi = _d(gross * (_d("3.25") / Decimal("100.0")))
        else:
            emp_esi = Decimal("0.00")
            empr_esi = Decimal("0.00")

        # Professional Tax (e.g. standard INR 200/month)
        prof_tax = _d(pt_amount)

        # Net Take Home Salary = Gross - Employee PF - Employee ESI - Professional Tax
        net_take_home = _d(gross - emp_pf - emp_esi - prof_tax)

        # Gather Statutory Citations
        citations = [
            self.kb.format_citation(c)
            for c in self.kb.get_citations_by_jurisdiction(Jurisdiction.INDIA)
            if "Provident" in c.act_name or "Insurance" in c.act_name or "Gratuity" in c.act_name
        ]

        return IndiaSalaryStructure(
            annual_ctc=float(ann_ctc),
            monthly_ctc=float(mon_ctc),
            basic_salary=float(basic),
            hra=float(hra),
            special_allowance=float(special_allowance),
            gross_salary=float(gross),
            employee_pf=float(emp_pf),
            employer_epf=float(employer_epf),
            employer_eps=float(employer_eps),
            employer_pf_total=float(employer_pf_total),
            employee_esi=float(emp_esi),
            employer_esi=float(empr_esi),
            professional_tax=float(prof_tax),
            gratuity_monthly_provision=float(gratuity_prov),
            net_take_home=float(net_take_home),
            statutory_citations=citations,
        )

    def structure_uae_salary(
        self,
        monthly_gross: float | Decimal,
        basic_percent: float = 60.0,
        housing_percent: float = 30.0,
        transport_percent: float = 10.0,
    ) -> UaeSalaryStructure:
        """Compute UAE salary breakdown according to MoHRE guidelines & Wages Protection System (WPS)."""
        gross = _d(monthly_gross)
        basic = _d(gross * (_d(basic_percent) / Decimal("100.0")))
        housing = _d(gross * (_d(housing_percent) / Decimal("100.0")))
        transport = _d(gross * (_d(transport_percent) / Decimal("100.0")))
        other = _d(gross - basic - housing - transport)
        if other < Decimal("0.00"):
            other = Decimal("0.00")

        # Monthly EOSB Accrual Provision (21 days / 365 days / 12 months)
        daily_basic = _d(basic / Decimal("30.0"))
        monthly_eosb = _d((daily_basic * Decimal("21.0")) / Decimal("12.0"))

        citations = [
            self.kb.format_citation(c)
            for c in self.kb.get_citations_by_jurisdiction(Jurisdiction.UAE)
        ]

        return UaeSalaryStructure(
            monthly_gross=float(gross),
            basic_wage=float(basic),
            housing_allowance=float(housing),
            transport_allowance=float(transport),
            other_allowances=float(other),
            wps_compliant=True,
            estimated_monthly_eosb_accrual=float(monthly_eosb),
            statutory_citations=citations,
        )

    def calculate_uae_eosb(
        self,
        basic_wage_monthly: float | Decimal,
        tenure_years: float,
    ) -> UaeEosbCalculation:
        """Compute End of Service Benefits (EOSB) under UAE Federal Decree-Law No. 33 of 2021 (Article 51).

        Rules:
        - Less than 1 year: 0 gratuity.
        - 1 to 5 years: 21 days' basic wage per year (using 21/30 monthly wage per year).
        - Above 5 years: 21 days/year for first 5 yrs + 30 days/year for additional years.
        - Maximum cap: 2 years' total basic wage (24 * monthly basic).
        """
        basic = _d(basic_wage_monthly)
        years = Decimal(str(tenure_years))

        if years < Decimal("1.0"):
            total_eosb = Decimal("0.00")
            breakdown = "Service tenure less than 1 year. No statutory gratuity entitled."
        elif years <= Decimal("5.0"):
            total_eosb = _d((basic * Decimal("21.0") * years) / Decimal("30.0"))
            breakdown = f"Tenure: {years:.2f} yrs. Calculation: ({basic} * 21 * {years:.2f}) / 30 = AED {total_eosb}"
        else:
            first_5_amount = _d((basic * Decimal("21.0") * Decimal("5.0")) / Decimal("30.0"))
            extra_years = years - Decimal("5.0")
            extra_amount = _d((basic * Decimal("30.0") * extra_years) / Decimal("30.0"))
            total_eosb = _d(first_5_amount + extra_amount)
            breakdown = (
                f"First 5 yrs (21 days/yr): AED {first_5_amount} + "
                f"Next {extra_years:.2f} yrs (30 days/yr): AED {extra_amount} = Total AED {total_eosb}"
            )

        # Statutory Cap: 2 years' basic wage = 24 * monthly basic
        max_cap = _d(basic * Decimal("24.0"))
        if total_eosb > max_cap:
            total_eosb = max_cap
            breakdown += f" (Capped at 2 years' basic wage statutory limit of AED {max_cap})"

        citation = self.kb.format_citation(
            [c for c in self.kb.get_citations_by_jurisdiction(Jurisdiction.UAE) if "Article 51" in c.section_or_article][0]
        )

        return UaeEosbCalculation(
            tenure_years=float(years),
            basic_wage_monthly=float(basic),
            total_eosb_gratuity=float(total_eosb),
            formula_breakdown=breakdown,
            statutory_citation=citation,
        )

