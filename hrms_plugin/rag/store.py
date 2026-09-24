"""Statutory Legal Knowledge Base and Hybrid Retriever for HRMS Labor Law Compliance."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class Jurisdiction(str, Enum):
    INDIA = "IN"
    UAE = "AE"
    SAUDI_ARABIA = "SA"
    USA = "US"
    GLOBAL = "GLOBAL"


@dataclass
class LegalCitation:
    jurisdiction: Jurisdiction
    act_name: str
    chapter_or_part: str
    section_or_article: str
    official_gazette_ref: str
    title: str
    summary: str
    statutory_rules: Dict[str, Any] = field(default_factory=dict)
    penalty_or_consequence: Optional[str] = None


# Comprehensive Statutory Corpus for Supported Jurisdictions
STATUTORY_CORPUS: List[LegalCitation] = [
    # =========================================================================
    # INDIA STATUTES
    # =========================================================================
    LegalCitation(
        jurisdiction=Jurisdiction.INDIA,
        act_name="The Code on Wages, 2019",
        chapter_or_part="Chapter II - Minimum Wages & Payment of Wages",
        section_or_article="Section 6 & Section 17",
        official_gazette_ref="Gazette of India, Extraordinary, Part II, Section 1, No. 42 (2019)",
        title="Payment of Minimum Wages and Time of Payment",
        summary=(
            "No employer shall pay to any employee wages less than the minimum rate of wages notified by the "
            "appropriate Government. Wages must be paid within 7 to 10 days of the wage period depending on "
            "enterprise size."
        ),
        statutory_rules={
            "min_wage_enforced": True,
            "wage_payment_deadline_days": 10,
            "overtime_rate_multiplier": 2.0,  # Overtime must be at least twice normal wage
        },
        penalty_or_consequence=(
            "Fine up to INR 50,000 for first offence; imprisonment up to 3 months for repeat offence."
        ),
    ),
    LegalCitation(
        jurisdiction=Jurisdiction.INDIA,
        act_name="Employees' Provident Funds and Miscellaneous Provisions Act, 1952",
        chapter_or_part="Scheme Para 29 & Para 30",
        section_or_article="Section 6 & EPF Scheme 1952",
        official_gazette_ref="EPFO Notification G.S.R. 608(E) / Act No. 19 of 1952",
        title="Provident Fund Contribution & Statutory Wage Ceilings",
        summary=(
            "Mandatory for establishments with >= 20 employees. Employee contributes 12% of Basic + DA. "
            "Employer contributes 12% (3.67% to EPF + 8.33% to EPS capped at statutory ceiling of INR 15,000/month)."
        ),
        statutory_rules={
            "employee_pf_percent": 12.0,
            "employer_pf_percent": 12.0,
            "employer_eps_percent": 8.33,
            "employer_epf_residual_percent": 3.67,
            "statutory_wage_ceiling_inr": 15000.0,
            "edli_admin_percent": 0.5,
            "pf_admin_percent": 0.5,
        },
        penalty_or_consequence=(
            "Penal damages under Section 14B up to 25% per annum + criminal liability under IPC 406/409."
        ),
    ),
    LegalCitation(
        jurisdiction=Jurisdiction.INDIA,
        act_name="Employees' State Insurance Act, 1948",
        chapter_or_part="Chapter IV - Contributions",
        section_or_article="Section 39 & Rule 51",
        official_gazette_ref="ESIC Notification No. S-38012/1/2016-SS.I (w.e.f. 01-07-2019)",
        title="ESI Social Security Coverage and Contribution Rates",
        summary=(
            "Applicable to employees earning gross wages up to INR 21,000/month. Employee contribution is 0.75% "
            "of gross wages; Employer contribution is 3.25% of gross wages."
        ),
        statutory_rules={
            "wage_ceiling_inr": 21000.0,
            "employee_esi_percent": 0.75,
            "employer_esi_percent": 3.25,
            "total_esi_percent": 4.00,
        },
        penalty_or_consequence="Prosecution under Section 85 with imprisonment up to 3 years and mandatory fines.",
    ),
    LegalCitation(
        jurisdiction=Jurisdiction.INDIA,
        act_name="Payment of Gratuity Act, 1972",
        chapter_or_part="Section 4 - Payment of Gratuity",
        section_or_article="Section 4(1), 4(2) & 4(3)",
        official_gazette_ref="Ministry of Labour & Employment Notification S.O. 1420(E)",
        title="Statutory Gratuity Entitlement and 15/26 Formula",
        summary=(
            "Payable to an employee on separation after continuous service of not less than 5 years. Calculated as "
            "(15 * Last Drawn Basic * Years of Service) / 26. Maximum tax-free ceiling is INR 2,000,000 (20 Lakhs)."
        ),
        statutory_rules={
            "vesting_years": 5.0,
            "days_factor": 15.0,
            "month_divisor": 26.0,
            "statutory_cap_inr": 2000000.0,
        },
        penalty_or_consequence="Simple interest at applicable bank rate + imprisonment under Section 9 up to 2 years.",
    ),
    LegalCitation(
        jurisdiction=Jurisdiction.INDIA,
        act_name="Maternity Benefit Act, 1961 (Amended 2017)",
        chapter_or_part="Section 5 - Right to Payment of Maternity Benefit",
        section_or_article="Section 5(3)",
        official_gazette_ref="Gazette of India, Extraordinary, Part II, Section 1, No. 6 (2017)",
        title="Mandatory Paid Maternity Leave Duration",
        summary=(
            "Entitles women employees to 26 weeks (182 days) of fully paid maternity leave for up to the first two "
            "surviving children (12 weeks for subsequent children)."
        ),
        statutory_rules={
            "paid_leave_weeks_first_two": 26.0,
            "paid_leave_weeks_subsequent": 12.0,
            "minimum_working_days_threshold": 80.0,
        },
        penalty_or_consequence="Imprisonment not less than 3 months extending up to 1 year and fines.",
    ),

    # =========================================================================
    # UAE STATUTES
    # =========================================================================
    LegalCitation(
        jurisdiction=Jurisdiction.UAE,
        act_name="Federal Decree-Law No. (33) of 2021 on the Regulation of Labour Relations",
        chapter_or_part="Chapter 2 - Employment Contracts & Working Hours",
        section_or_article="Article 9 & Article 17",
        official_gazette_ref="UAE Official Gazette No. 716 (30 Nov 2021)",
        title="Probationary Period and Working Hours Limits",
        summary=(
            "Probation period must not exceed 6 months (180 days). Ordinary working hours shall be a maximum of "
            "8 hours per day or 48 hours per week."
        ),
        statutory_rules={
            "max_probation_months": 6.0,
            "max_probation_days": 180,
            "max_daily_hours": 8.0,
            "max_weekly_hours": 48.0,
            "max_daily_overtime_hours": 2.0,
        },
        penalty_or_consequence="MoHRE administrative fines from AED 5,000 to AED 1,000,000.",
    ),
    LegalCitation(
        jurisdiction=Jurisdiction.UAE,
        act_name="Federal Decree-Law No. (33) of 2021 on Labour Relations",
        chapter_or_part="Chapter 7 - End of Service Benefits (EOSB)",
        section_or_article="Article 51",
        official_gazette_ref="Cabinet Resolution No. (1) of 2022 on Executive Regulations",
        title="End of Service Severance Gratuity (EOSB)",
        summary=(
            "Full-time foreign employees completing 1+ years of service are entitled to severance gratuity: 21 days' "
            "basic wage for each year of the first 5 years, and 30 days' basic wage for each additional year, capped "
            "at 2 years' total wage."
        ),
        statutory_rules={
            "min_service_years": 1.0,
            "days_per_year_first_5": 21.0,
            "days_per_year_after_5": 30.0,
            "maximum_cap_multiplier_years": 2.0,
            "daily_wage_divisor": 30.0,
        },
        penalty_or_consequence="Financial claims subject to MoHRE fast-track labor tribunal.",
    ),
    LegalCitation(
        jurisdiction=Jurisdiction.UAE,
        act_name="Ministerial Resolution No. 43 of 2022 on Wages Protection System (WPS)",
        chapter_or_part="WPS Compliance Framework",
        section_or_article="Article 3 & Article 4",
        official_gazette_ref="MoHRE Ministerial Decree No. 43/2022",
        title="Wages Protection System (WPS) Timely Transfer and Minimum Quotas",
        summary=(
            "All employers registered with MoHRE must pay worker wages via the authorized WPS channel within 15 days "
            "of the due date. At least 90% of total employees must receive wages monthly to avoid license suspension."
        ),
        statutory_rules={
            "wps_transfer_deadline_days": 15,
            "min_workforce_paid_percentage": 90.0,
            "min_wage_paid_percentage_per_worker": 80.0,
        },
        penalty_or_consequence=(
            "Automated block on new work permits, license freeze, and fines of AED 1,000 per delayed worker."
        ),
    ),

    # =========================================================================
    # SAUDI ARABIA STATUTES
    # =========================================================================
    LegalCitation(
        jurisdiction=Jurisdiction.SAUDI_ARABIA,
        act_name="Saudi Labour Law (Royal Decree No. M/51)",
        chapter_or_part="Part VI - Working Conditions & Terms of Employment",
        section_or_article="Article 98, Article 107 & Article 84",
        official_gazette_ref="Umm Al-Qura Official Gazette Issue No. 4068",
        title="Working Hours, Overtime Multipliers, and End-of-Service Award",
        summary=(
            "Standard working hours: 8 hours/day, 48 hours/week (reduced to 6 hours/day, 36 hours/week during Ramadan "
            "for Muslims). Overtime paid at 100% basic + 50% extra (1.5x). End of service: half month wage for each "
            "of first 5 years, full month wage for subsequent years."
        ),
        statutory_rules={
            "max_daily_hours": 8.0,
            "max_weekly_hours": 48.0,
            "ramadan_daily_hours": 6.0,
            "overtime_multiplier": 1.5,
            "eosb_first_5_factor": 0.5,  # half month per year
            "eosb_subsequent_factor": 1.0,  # full month per year
        },
        penalty_or_consequence=(
            "Ministry of Human Resources and Social Development (MHRSD) fines up to "
            "SAR 100,000 and temporary portal block."
        ),
    ),

    # =========================================================================
    # UNITED STATES STATUTES
    # =========================================================================
    LegalCitation(
        jurisdiction=Jurisdiction.USA,
        act_name="Fair Labor Standards Act (FLSA)",
        chapter_or_part="29 U.S.C. Chapter 8",
        section_or_article="Section 207 (Maximum Hours & Overtime)",
        official_gazette_ref="29 U.S.C. § 201 et seq.; 29 CFR Part 541",
        title="FLSA Non-Exempt Overtime and Minimum Wage Requirements",
        summary=(
            "Non-exempt employees must receive overtime pay for hours worked over 40 in a workweek at a rate not "
            "less than time and one-half (1.5x) their regular rate of pay. Federal minimum wage standard applies."
        ),
        statutory_rules={
            "standard_workweek_hours": 40.0,
            "overtime_multiplier": 1.5,
            "federal_minimum_wage_usd": 7.25,
            "exempt_salary_threshold_annual_usd": 43888.0,  # 2024 DOL Rule standard
        },
        penalty_or_consequence=(
            "Back wages + 100% liquidated damages (2x total unpaid amount) + attorney fees under 29 U.S.C. § 216(b)."
        ),
    ),
]


class StatutoryKnowledgeBase:
    """In-memory hybrid statutory retriever with jurisdiction filtering and keyword indexing."""

    def __init__(self, corpus: Optional[List[LegalCitation]] = None):
        self.corpus = corpus or STATUTORY_CORPUS

    def get_citations_by_jurisdiction(self, jurisdiction: Jurisdiction | str) -> List[LegalCitation]:
        jur_val = jurisdiction.value if isinstance(jurisdiction, Jurisdiction) else str(jurisdiction).upper()
        return [c for c in self.corpus if c.jurisdiction.value == jur_val or c.jurisdiction == Jurisdiction.GLOBAL]

    def search(
        self,
        query: str,
        jurisdiction: Optional[Jurisdiction | str] = None,
        limit: int = 5,
    ) -> List[LegalCitation]:
        """Perform keyword and semantic token search across legal citations."""
        results = self.corpus
        if jurisdiction:
            jur_val = jurisdiction.value if isinstance(jurisdiction, Jurisdiction) else str(jurisdiction).upper()
            results = [c for c in results if c.jurisdiction.value == jur_val or c.jurisdiction == Jurisdiction.GLOBAL]

        # Tokenize query
        tokens = [t.lower() for t in re.findall(r"\w+", query) if len(t) > 2]
        if not tokens:
            return results[:limit]

        scored: List[tuple[float, LegalCitation]] = []
        for citation in results:
            score = 0.0
            searchable_text = (
                f"{citation.act_name} {citation.title} {citation.summary} "
                f"{citation.section_or_article} {citation.chapter_or_part}"
            ).lower()

            for token in tokens:
                if token in searchable_text:
                    # Title & Act matches have higher weight
                    if token in citation.act_name.lower():
                        score += 3.0
                    if token in citation.title.lower():
                        score += 2.0
                    if token in citation.section_or_article.lower():
                        score += 2.5
                    score += 1.0

            if score > 0:
                scored.append((score, citation))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [c for _, c in scored[:limit]]

    def format_citation(self, citation: LegalCitation) -> str:
        """Format a LegalCitation into an enterprise-grade compliance footnote."""
        return (
            f"[{citation.jurisdiction.value}] {citation.act_name}, {citation.section_or_article} "
            f"({citation.chapter_or_part}). Ref: {citation.official_gazette_ref}. Title: '{citation.title}'"
        )
