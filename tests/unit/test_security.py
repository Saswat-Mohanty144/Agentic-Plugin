"""Unit tests for Zero-Trust Security, PII Masking, RBAC, and Audit Attributions."""

import pytest

from hrms_plugin.security.audit import HostAuditGateway
from hrms_plugin.security.masking import PiiMaskingGateway
from hrms_plugin.security.rbac import (
    AuthContext,
    Permission,
    RbacPolicyEnforcer,
    UserRole,
)


@pytest.fixture
def pii_gateway():
    return PiiMaskingGateway()


@pytest.fixture
def rbac_enforcer():
    return RbacPolicyEnforcer()


@pytest.fixture
def audit_gateway():
    return HostAuditGateway()


def test_pii_masking_aadhaar_pan_emirates_id(pii_gateway):
    """Verify that national IDs are redacted with vault tokens and can be de-tokenized."""
    raw_prompt = (
        "Employee John Doe with Aadhaar 5412 8931 0124 and PAN ABCDE1234F "
        "and Emirates ID 784-1990-1234567-1 has requested compensation review."
    )

    masked, tokens = pii_gateway.mask_text(raw_prompt, tenant_id="TENANT-01")

    assert "5412 8931 0124" not in masked
    assert "ABCDE1234F" not in masked
    assert "784-1990-1234567-1" not in masked
    assert "[VAULT_AADHAAR_" in masked
    assert "[VAULT_PAN_" in masked
    assert "[VAULT_EMIRATES_ID_" in masked
    assert len(tokens) == 3

    # Verify unmasking on egress
    restored = pii_gateway.unmask_text(masked, tenant_id="TENANT-01")
    assert restored == raw_prompt


def test_pii_mask_record_dict(pii_gateway):
    """Verify dictionary masking for compensation figures and national ID fields."""
    record = {
        "employee_id": "EMP-401",
        "first_name": "Sarah",
        "salary": 185000.0,
        "bank_account": "912384729104",
        "department": "Engineering",
    }

    masked_rec = pii_gateway.mask_record_dict(record, tenant_id="TENANT-01")

    assert masked_rec["employee_id"] == "EMP-401"
    assert masked_rec["department"] == "Engineering"
    assert masked_rec["salary"].startswith("[VAULT_SALARY_")
    assert masked_rec["bank_account"].startswith("[VAULT_BANK_ACCOUNT_")


def test_rbac_authorization(rbac_enforcer):
    """Test RBAC role-to-permission mapping."""
    hr_admin = AuthContext(user_id="USR-HR", tenant_id="T1", role=UserRole.HR_ADMIN)
    employee = AuthContext(user_id="USR-EMP", tenant_id="T1", role=UserRole.EMPLOYEE)

    assert rbac_enforcer.is_authorized(hr_admin, Permission.MUTATE_EMPLOYEE) is True
    assert rbac_enforcer.is_authorized(hr_admin, Permission.EXECUTE_REMEDIATION) is True

    # Regular employee cannot mutate salary or execute compliance remediation
    assert rbac_enforcer.is_authorized(employee, Permission.MUTATE_SALARY) is False
    assert rbac_enforcer.is_authorized(employee, Permission.EXECUTE_REMEDIATION) is False
    assert rbac_enforcer.is_authorized(employee, Permission.APPLY_LEAVE) is True


def test_separation_of_duties_self_approval(rbac_enforcer):
    """Test prevention of self-approval for leave and salary mutations."""
    manager = AuthContext(user_id="MGR-001", tenant_id="T1", role=UserRole.DEPARTMENT_MANAGER)

    # Manager approving peer's leave -> Allowed
    assert rbac_enforcer.validate_separation_of_duties(
        manager, target_employee_id="EMP-002", action=Permission.APPROVE_LEAVE
    ) is True

    # Manager approving their own leave -> Blocked
    assert rbac_enforcer.validate_separation_of_duties(
        manager, target_employee_id="MGR-001", action=Permission.APPROVE_LEAVE
    ) is False


def test_host_audit_headers_creation(audit_gateway):
    """Test generation of enterprise action attribution HTTP headers."""
    reasoning = {
        "rule": "UAE_MAX_PROBATION_EXCEEDED",
        "action": "CAP_PROBATION_180_DAYS",
        "target_employee": "EMP-401",
    }

    headers = audit_gateway.create_attribution_headers(
        tenant_id="TENANT-DXB",
        agent_name="StatutoryComplianceAuditor",
        hitl_approver_id="USR-MGR-99",
        reasoning_payload=reasoning,
    )

    http_dict = headers.to_http_headers()

    assert http_dict["X-Plugin-Version"] == "2.0.0"
    assert http_dict["X-Agent-Name"] == "StatutoryComplianceAuditor"
    assert http_dict["X-HITL-Approver-Id"] == "USR-MGR-99"
    assert http_dict["X-Tenant-Id"] == "TENANT-DXB"
    assert http_dict["X-Reasoning-Hash"].startswith("sha256:")
