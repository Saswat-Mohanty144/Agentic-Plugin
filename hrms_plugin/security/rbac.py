"""Role-Based Access Control (RBAC) and Separation of Duties Governance."""

from __future__ import annotations

from enum import Enum
from typing import Dict, FrozenSet, Optional, Set

from pydantic import BaseModel, ConfigDict


class UserRole(str, Enum):
    SUPER_ADMIN = "SUPER_ADMIN"
    HR_ADMIN = "HR_ADMIN"
    PAYROLL_ADMIN = "PAYROLL_ADMIN"
    HIRING_MANAGER = "HIRING_MANAGER"
    DEPARTMENT_MANAGER = "DEPARTMENT_MANAGER"
    EMPLOYEE = "EMPLOYEE"


class Permission(str, Enum):
    VIEW_EMPLOYEE = "VIEW_EMPLOYEE"
    MUTATE_EMPLOYEE = "MUTATE_EMPLOYEE"
    VIEW_SALARY = "VIEW_SALARY"
    MUTATE_SALARY = "MUTATE_SALARY"
    RUN_PAYROLL = "RUN_PAYROLL"
    APPLY_LEAVE = "APPLY_LEAVE"
    APPROVE_LEAVE = "APPROVE_LEAVE"
    VIEW_COMPLIANCE = "VIEW_COMPLIANCE"
    EXECUTE_REMEDIATION = "EXECUTE_REMEDIATION"
    MANAGE_RECRUITMENT = "MANAGE_RECRUITMENT"
    USE_COPILOT = "USE_COPILOT"
    MANAGE_SCHEMA = "MANAGE_SCHEMA"
    APPROVE_EXPENSE = "APPROVE_EXPENSE"


ROLE_PERMISSIONS: Dict[UserRole, Set[Permission]] = {
    UserRole.SUPER_ADMIN: set(Permission),
    UserRole.HR_ADMIN: {
        Permission.VIEW_EMPLOYEE,
        Permission.MUTATE_EMPLOYEE,
        Permission.VIEW_SALARY,
        Permission.APPLY_LEAVE,
        Permission.APPROVE_LEAVE,
        Permission.VIEW_COMPLIANCE,
        Permission.EXECUTE_REMEDIATION,
        Permission.MANAGE_RECRUITMENT,
        Permission.USE_COPILOT,
        Permission.APPROVE_EXPENSE,
    },
    UserRole.PAYROLL_ADMIN: {
        Permission.VIEW_EMPLOYEE,
        Permission.VIEW_SALARY,
        Permission.MUTATE_SALARY,
        Permission.RUN_PAYROLL,
        Permission.VIEW_COMPLIANCE,
        Permission.EXECUTE_REMEDIATION,
        Permission.USE_COPILOT,
        Permission.APPROVE_EXPENSE,
    },
    UserRole.HIRING_MANAGER: {
        Permission.VIEW_EMPLOYEE,
        Permission.MANAGE_RECRUITMENT,
        Permission.USE_COPILOT,
    },
    UserRole.DEPARTMENT_MANAGER: {
        Permission.VIEW_EMPLOYEE,
        Permission.APPLY_LEAVE,
        Permission.APPROVE_LEAVE,
        Permission.USE_COPILOT,
        Permission.APPROVE_EXPENSE,
    },
    UserRole.EMPLOYEE: {
        Permission.VIEW_EMPLOYEE,
        Permission.APPLY_LEAVE,
        Permission.USE_COPILOT,
    },
}


class AuthContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: str
    tenant_id: str
    roles: FrozenSet[UserRole] = frozenset()
    issued_at: Optional[float] = None
    token_id: Optional[str] = None
    email: Optional[str] = None


class PermissionDenied(Exception):
    """Raised when an authenticated caller lacks the permission an action requires."""

    def __init__(self, permission: Permission):
        super().__init__(permission.value)
        self.permission = permission


class RbacPolicyEnforcer:
    """Zero-Trust RBAC and Separation of Duties policy enforcement engine."""

    def is_authorized(self, auth: AuthContext, permission: Permission) -> bool:
        """True if any of the caller's roles grants the permission."""
        return any(permission in ROLE_PERMISSIONS.get(role, set()) for role in auth.roles)

    def validate_separation_of_duties(
        self,
        auth: AuthContext,
        target_employee_id: str,
        action: Permission,
    ) -> bool:
        """Enforce strict separation of duties (e.g. self-approval prevention)."""
        # A manager cannot approve their own leave request
        if action == Permission.APPROVE_LEAVE and auth.user_id == target_employee_id:
            return False

        # A payroll admin cannot approve their own salary structure changes
        if action == Permission.MUTATE_SALARY and auth.user_id == target_employee_id:
            return False

        return True
