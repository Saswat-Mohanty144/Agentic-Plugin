"""Unit tests for Compliance Auditor Agent."""

import pytest

from hrms_plugin.agents.compliance import ComplianceAgent, ViolationSeverity
from hrms_plugin.rag.store import Jurisdiction


@pytest.fixture
def compliance_agent():
    return ComplianceAgent()


def test_uae_probation_period_audit(compliance_agent):
    """Test detection of illegal probation duration (> 180 days) under UAE labor law."""
    employees = [
        {"id": "EMP-001", "first_name": "Ahmed", "last_name": "Ali", "probation_days": 120},  # Compliant
        {"id": "EMP-002", "first_name": "Tariq", "last_name": "Mansoor", "probation_days": 240},  # Non-compliant
    ]

    report = compliance_agent.audit_employees(
        tenant_id="TENANT-DXB", jurisdiction=Jurisdiction.UAE, employees=employees
    )

    assert report.is_compliant is False
    assert report.violations_count == 1
    assert report.high_count == 1

    vio = report.violations[0]
    assert vio.rule_key == "UAE_MAX_PROBATION_EXCEEDED"
    assert vio.entity_id == "EMP-002"
    assert vio.severity == ViolationSeverity.HIGH
    assert "Article 9" in vio.statutory_citation
    assert vio.remediation_patch == {"probation_days": 180}


def test_india_mandatory_pf_audit(compliance_agent):
    """Test detection of missing statutory PF enrollment for low-wage earners in India."""
    employees = [
        {"id": "EMP-101", "first_name": "Ramesh", "basic_salary": 14000.0, "is_pf_enrolled": False},  # Non-compliant
        {"id": "EMP-102", "first_name": "Suresh", "basic_salary": 14000.0, "is_pf_enrolled": True},  # Compliant
    ]

    report = compliance_agent.audit_employees(
        tenant_id="TENANT-BLR", jurisdiction=Jurisdiction.INDIA, employees=employees
    )

    assert report.is_compliant is False
    assert report.violations_count == 1
    assert report.critical_count == 1
    assert report.violations[0].rule_key == "INDIA_MANDATORY_PF_MISSING"
    assert "Provident Funds" in report.violations[0].statutory_citation


def test_uae_overtime_daily_cap_audit(compliance_agent):
    """Test detection of excessive daily working hours (> 10h) under UAE Article 17."""
    punches = [
        {"employee_id": "EMP-001", "work_date": "2026-09-20", "total_hours": 8.5},
        {"employee_id": "EMP-002", "work_date": "2026-09-20", "total_hours": 12.0},  # Violation (>10h)
    ]

    report = compliance_agent.audit_attendance_and_overtime(
        tenant_id="TENANT-DXB", jurisdiction=Jurisdiction.UAE, punch_records=punches
    )

    assert report.is_compliant is False
    assert report.violations_count == 1
    assert report.violations[0].rule_key == "UAE_MAX_DAILY_HOURS_EXCEEDED"
    assert "Article 17" in report.violations[0].statutory_citation


def test_uae_wps_payroll_batch_audit(compliance_agent):
    """Test detection of sub-90% WPS payroll coverage and delayed payment."""
    batch = {
        "batch_id": "BATCH-SEP-2026",
        "total_headcount": 100,
        "paid_headcount": 82,  # 82% < 90% threshold
        "days_since_period_end": 18,  # > 15 days delay
    }

    report = compliance_agent.audit_wps_payroll_batch(
        tenant_id="TENANT-DXB", jurisdiction=Jurisdiction.UAE, payroll_batch=batch
    )

    assert report.is_compliant is False
    assert report.violations_count == 2
    assert report.critical_count == 2
    rule_keys = [v.rule_key for v in report.violations]
    assert "UAE_WPS_SUB_90_PERCENT_QUOTA" in rule_keys
    assert "UAE_WPS_PAYMENT_DELAYED" in rule_keys
