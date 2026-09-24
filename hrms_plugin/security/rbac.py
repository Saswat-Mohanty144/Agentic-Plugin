"""Role-Based Access Control (RBAC) and Separation of Duties Governance."""

from __future__ import annotations

from enum import Enum
from typing import Dict, List, Optional, Set
from pydantic import BaseModel


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
    },
    UserRole.PAYROLL_ADMIN: {
        Permission.VIEW_EMPLOYEE,
        Permission.VIEW_SALARY,
        Permission.MUTATE_SALARY,
        Permission.RUN_PAYROLL,
        Permission.VIEW_COMPLIANCE,
        Permission.EXECUTE_REMEDIATION,
    },
    UserRole.HIRING_MANAGER: {
        Permission.VIEW_EMPLOYEE,
        Permission.MANAGE_RECRUITMENT,
    },
    UserRole.DEPARTMENT_MANAGER: {
        Permission.VIEW_EMPLOYEE,
        Permission.APPLY_LEAVE,
        Permission.APPROVE_LEAVE,
    },
    UserRole.EMPLOYEE: {
        Permission.VIEW_EMPLOYEE,
        Permission.APPLY_LEAVE,
    },
}


class AuthContext(BaseModel):
    user_id: str
    tenant_id: str
    role: UserRole
    email: Optional[str] = None


class RbacPolicyEnforcer:
    """Zero-Trust RBAC and Separation of Duties policy enforcement engine."""

    def is_authorized(self, auth: AuthContext, permission: Permission) -> bool:
        """Check if user role possesses the required permission."""
        allowed = ROLE_PERMISSIONS.get(auth.role, set())
        return permission in allowed

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
