"""Statutory Labor Compliance Auditor Agent with Mandatory Gazette Citations."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from hrms_plugin.rag.store import Jurisdiction, StatutoryKnowledgeBase


class ViolationSeverity(str, Enum):
    CRITICAL = "CRITICAL"  # Statutory penalty, legal liability, wage protection suspension
    HIGH = "HIGH"          # Labor code non-conformance, overtime cap breach
    MEDIUM = "MEDIUM"      # Documentation / policy discrepancy
    LOW = "LOW"            # Advisory / best-practice warning


class ComplianceViolation(BaseModel):
    violation_id: str
    rule_key: str
    severity: ViolationSeverity
    jurisdiction: Jurisdiction
    entity_type: str  # "EMPLOYEE", "PAYROLL", "PUNCH_LOG", "LEAVE"
    entity_id: str
    summary: str
    detailed_findings: str
    statutory_citation: str
    penalty_risk: Optional[str] = None
    remediation_recommendation: str
    remediation_patch: Optional[Dict[str, Any]] = None


class ComplianceAuditReport(BaseModel):
    tenant_id: str
    jurisdiction: Jurisdiction
    total_records_audited: int
    violations_count: int
    critical_count: int
    high_count: int
    is_compliant: bool
    violations: List[ComplianceViolation] = Field(default_factory=list)


class ComplianceAgent:
    """Specialist agent responsible for auditing HR transactions against statutory labor codes."""

    def __init__(self, kb: Optional[StatutoryKnowledgeBase] = None):
        self.kb = kb or StatutoryKnowledgeBase()

    def audit_employees(
        self,
        tenant_id: str,
        jurisdiction: Jurisdiction | str,
        employees: List[Dict[str, Any]],
    ) -> ComplianceAuditReport:
        """Audit employee master records against statutory probation limits, social security enrollments, etc."""
        jur = Jurisdiction(jurisdiction) if isinstance(jurisdiction, str) else jurisdiction
        violations: List[ComplianceViolation] = []

        for emp in employees:
            emp_id = str(emp.get("id") or emp.get("employee_id") or "UNKNOWN")

            # 1. Check Probation Period (UAE: max 6 months / 180 days)
            if jur == Jurisdiction.UAE:
                probation_days = emp.get("probation_days") or emp.get("probation_period_days")
                if probation_days and float(probation_days) > 180:
                    citations = [
                        c for c in self.kb.get_citations_by_jurisdiction(Jurisdiction.UAE)
                        if "Article 9" in c.section_or_article
                    ]
                    cit_str = (
                        self.kb.format_citation(citations[0])
                        if citations
                        else "UAE Federal Decree-Law No. 33 of 2021, Article 9"
                    )
                    violations.append(
                        ComplianceViolation(
                            violation_id=f"VIO-PROB-{emp_id}",
                            rule_key="UAE_MAX_PROBATION_EXCEEDED",
                            severity=ViolationSeverity.HIGH,
                            jurisdiction=jur,
                            entity_type="EMPLOYEE",
                            entity_id=emp_id,
                            summary=(
                                f"Probation period of {probation_days} days exceeds statutory "
                                "maximum of 6 months (180 days)."
                            ),
                            detailed_findings=(
                                f"Employee {emp.get('first_name', '')} {emp.get('last_name', '')} has probation "
                                f"set to {probation_days} days. UAE Labor Law strictly caps probation at 180 days."
                            ),
                            statutory_citation=cit_str,
                            penalty_risk="Administrative fines from MoHRE up to AED 10,000.",
                            remediation_recommendation="Reduce probation period to statutory limit of 180 days.",
                            remediation_patch={"probation_days": 180},
                        )
                    )

            # 2. Check Mandatory PF Enrollment (India: Basic <= 15000 or general mandatory PF)
            if jur == Jurisdiction.INDIA:
                basic = float(emp.get("basic_salary") or emp.get("basic") or 0.0)
                is_pf_enrolled = emp.get("is_pf_enrolled", emp.get("pf_applicable", True))
                if basic > 0 and basic <= 15000 and not is_pf_enrolled:
                    citations = [
                        c for c in self.kb.get_citations_by_jurisdiction(Jurisdiction.INDIA)
                        if "Provident Funds" in c.act_name
                    ]
                    cit_str = (
                        self.kb.format_citation(citations[0])
                        if citations
                        else "EPF & MP Act, 1952, Section 6"
                    )
                    violations.append(
                        ComplianceViolation(
                            violation_id=f"VIO-PF-{emp_id}",
                            rule_key="INDIA_MANDATORY_PF_MISSING",
                            severity=ViolationSeverity.CRITICAL,
                            jurisdiction=jur,
                            entity_type="EMPLOYEE",
                            entity_id=emp_id,
                            summary="Employee earning <= INR 15,000 basic is not enrolled in statutory Provident Fund.",
                            detailed_findings=(
                                f"Employee has monthly basic wage INR {basic:.2f} (< 15,000 threshold) "
                                "but PF coverage is disabled."
                            ),
                            statutory_citation=cit_str,
                            penalty_risk="Penal damages under Section 14B up to 25% + IPC 406 liability.",
                            remediation_recommendation=(
                                "Enable statutory EPF contribution (12% employee + 12% employer)."
                            ),
                            remediation_patch={"is_pf_enrolled": True, "pf_applicable": True},
                        )
                    )

        crit = sum(1 for v in violations if v.severity == ViolationSeverity.CRITICAL)
        high = sum(1 for v in violations if v.severity == ViolationSeverity.HIGH)

        return ComplianceAuditReport(
            tenant_id=tenant_id,
            jurisdiction=jur,
            total_records_audited=len(employees),
            violations_count=len(violations),
            critical_count=crit,
            high_count=high,
            is_compliant=(len(violations) == 0),
            violations=violations,
        )

    def audit_attendance_and_overtime(
        self,
        tenant_id: str,
        jurisdiction: Jurisdiction | str,
        punch_records: List[Dict[str, Any]],
    ) -> ComplianceAuditReport:
        """Audit daily and weekly working hours for overtime limits and compliance."""
        jur = Jurisdiction(jurisdiction) if isinstance(jurisdiction, str) else jurisdiction
        violations: List[ComplianceViolation] = []

        for punch in punch_records:
            emp_id = str(punch.get("employee_id") or punch.get("emp_id") or "UNKNOWN")
            daily_hours = float(punch.get("total_hours") or punch.get("hours_worked") or 0.0)
            date_str = str(punch.get("work_date") or punch.get("date") or "UNKNOWN_DATE")

            # Check Overtime Caps (UAE: max 2 hours overtime on top of 8h standard = 10h max per day)
            if jur == Jurisdiction.UAE and daily_hours > 10.0:
                citations = [
                    c for c in self.kb.get_citations_by_jurisdiction(Jurisdiction.UAE)
                    if "Article 17" in c.section_or_article
                ]
                cit_str = (
                    self.kb.format_citation(citations[0])
                    if citations
                    else "UAE Federal Decree-Law No. 33 of 2021, Article 17"
                )
                violations.append(
                    ComplianceViolation(
                        violation_id=f"VIO-OT-{emp_id}-{date_str}",
                        rule_key="UAE_MAX_DAILY_HOURS_EXCEEDED",
                        severity=ViolationSeverity.HIGH,
                        jurisdiction=jur,
                        entity_type="PUNCH_LOG",
                        entity_id=emp_id,
                        summary=(
                            f"Daily working hours of {daily_hours}h exceeds statutory cap of 10 hours "
                            "(8h standard + 2h max overtime)."
                        ),
                        detailed_findings=(
                            f"Employee logged {daily_hours} hours on {date_str}. UAE Article 17 "
                            "prohibits daily working time exceeding 10 hours including overtime."
                        ),
                        statutory_citation=cit_str,
                        penalty_risk="MoHRE labor inspector citation and company fine.",
                        remediation_recommendation="Cap payable regular + overtime hours at 10 hours and issue alert.",
                        remediation_patch={"total_hours": 10.0, "excess_overtime_flag": True},
                    )
                )

        crit = sum(1 for v in violations if v.severity == ViolationSeverity.CRITICAL)
        high = sum(1 for v in violations if v.severity == ViolationSeverity.HIGH)

        return ComplianceAuditReport(
            tenant_id=tenant_id,
            jurisdiction=jur,
            total_records_audited=len(punch_records),
            violations_count=len(violations),
            critical_count=crit,
            high_count=high,
            is_compliant=(len(violations) == 0),
            violations=violations,
        )

    def audit_wps_payroll_batch(
        self,
        tenant_id: str,
        jurisdiction: Jurisdiction | str,
        payroll_batch: Dict[str, Any],
    ) -> ComplianceAuditReport:
        """Audit payroll batch for Wages Protection System (WPS) compliance and salary timeliness."""
        jur = Jurisdiction(jurisdiction) if isinstance(jurisdiction, str) else jurisdiction
        violations: List[ComplianceViolation] = []

        total_employees = int(payroll_batch.get("total_headcount") or len(payroll_batch.get("employees", [])) or 1)
        paid_employees = int(payroll_batch.get("paid_headcount") or total_employees)
        days_after_cutoff = int(payroll_batch.get("days_since_period_end") or 0)

        if jur == Jurisdiction.UAE:
            # 1. 90% workforce threshold check
            paid_pct = (paid_employees / total_employees) * 100.0 if total_employees > 0 else 100.0
            if paid_pct < 90.0:
                citations = [
                    c for c in self.kb.get_citations_by_jurisdiction(Jurisdiction.UAE)
                    if "Wages Protection" in c.act_name
                ]
                cit_str = (
                    self.kb.format_citation(citations[0])
                    if citations
                    else "Ministerial Resolution No. 43 of 2022 on WPS"
                )
                violations.append(
                    ComplianceViolation(
                        violation_id=f"VIO-WPS-QUOTA-{tenant_id}",
                        rule_key="UAE_WPS_SUB_90_PERCENT_QUOTA",
                        severity=ViolationSeverity.CRITICAL,
                        jurisdiction=jur,
                        entity_type="PAYROLL",
                        entity_id=str(payroll_batch.get("batch_id", "BATCH-01")),
                        summary=(
                            f"WPS payment coverage is {paid_pct:.1f}%, failing the statutory 90% minimum threshold."
                        ),
                        detailed_findings=(
                            f"Only {paid_employees} of {total_employees} employees are included in disbursement file."
                        ),
                        statutory_citation=cit_str,
                        penalty_risk="Immediate block on MoHRE portal for issuing new work permits & company fines.",
                        remediation_recommendation=(
                            "Include missing active employees in the SIF (Salary Information File) generation."
                        ),
                        remediation_patch={"include_all_active_employees": True},
                    )
                )

            # 2. 15-day deadline check
            if days_after_cutoff > 15:
                citations = [
                    c for c in self.kb.get_citations_by_jurisdiction(Jurisdiction.UAE)
                    if "Wages Protection" in c.act_name
                ]
                cit_str = (
                    self.kb.format_citation(citations[0])
                    if citations
                    else "Ministerial Resolution No. 43 of 2022 on WPS"
                )
                violations.append(
                    ComplianceViolation(
                        violation_id=f"VIO-WPS-DELAY-{tenant_id}",
                        rule_key="UAE_WPS_PAYMENT_DELAYED",
                        severity=ViolationSeverity.CRITICAL,
                        jurisdiction=jur,
                        entity_type="PAYROLL",
                        entity_id=str(payroll_batch.get("batch_id", "BATCH-01")),
                        summary=f"Payroll disbursement is delayed by {days_after_cutoff} days (> 15-day limit).",
                        detailed_findings=(
                            "MoHRE requires wages to be transferred within 15 days from the period due date."
                        ),
                        statutory_citation=cit_str,
                        penalty_risk=(
                            "Administrative fine of AED 1,000 per delayed worker and corporate rating downgrade."
                        ),
                        remediation_recommendation=(
                            "Execute SIF bank transfer immediately to avoid automated sanctions."
                        ),
                    )
                )

        crit = sum(1 for v in violations if v.severity == ViolationSeverity.CRITICAL)
        high = sum(1 for v in violations if v.severity == ViolationSeverity.HIGH)

        return ComplianceAuditReport(
            tenant_id=tenant_id,
            jurisdiction=jur,
            total_records_audited=total_employees,
            violations_count=len(violations),
            critical_count=crit,
            high_count=high,
            is_compliant=(len(violations) == 0),
            violations=violations,
        )
