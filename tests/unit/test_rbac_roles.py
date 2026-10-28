"""Role grants, multi-role union and PermissionDenied (spec §6.2, §6.4)."""

import pytest

from hrms_plugin.security.rbac import AuthContext, PermissionDenied, RbacPolicyEnforcer
from hrms_plugin.security.rbac import Permission as P
from hrms_plugin.security.rbac import UserRole as R

GRANTS = {
    R.HR_ADMIN: {P.VIEW_EMPLOYEE, P.MUTATE_EMPLOYEE, P.VIEW_SALARY, P.APPLY_LEAVE, P.APPROVE_LEAVE,
                 P.VIEW_COMPLIANCE, P.EXECUTE_REMEDIATION, P.MANAGE_RECRUITMENT, P.USE_COPILOT, P.APPROVE_EXPENSE},
    R.PAYROLL_ADMIN: {P.VIEW_EMPLOYEE, P.VIEW_SALARY, P.MUTATE_SALARY, P.RUN_PAYROLL, P.VIEW_COMPLIANCE,
                      P.EXECUTE_REMEDIATION, P.USE_COPILOT, P.APPROVE_EXPENSE},
    R.HIRING_MANAGER: {P.VIEW_EMPLOYEE, P.MANAGE_RECRUITMENT, P.USE_COPILOT},
    R.DEPARTMENT_MANAGER: {P.VIEW_EMPLOYEE, P.APPLY_LEAVE, P.APPROVE_LEAVE, P.USE_COPILOT, P.APPROVE_EXPENSE},
    R.EMPLOYEE: {P.VIEW_EMPLOYEE, P.APPLY_LEAVE, P.USE_COPILOT},
    R.SUPER_ADMIN: set(P),
}


def ctx(*roles):
    return AuthContext(user_id="u", tenant_id="acme", roles=frozenset(roles))


@pytest.mark.parametrize("role", list(R))
@pytest.mark.parametrize("perm", list(P))
def test_role_grant_matrix(role, perm):
    assert RbacPolicyEnforcer().is_authorized(ctx(role), perm) == (perm in GRANTS[role])


def test_manage_schema_is_super_admin_only():
    holders = {r for r in R if RbacPolicyEnforcer().is_authorized(ctx(r), P.MANAGE_SCHEMA)}
    assert holders == {R.SUPER_ADMIN}


def test_union_of_roles():
    assert RbacPolicyEnforcer().is_authorized(ctx(R.EMPLOYEE, R.HIRING_MANAGER), P.MANAGE_RECRUITMENT)


def test_no_roles_has_no_permissions():
    assert not any(RbacPolicyEnforcer().is_authorized(ctx(), p) for p in P)


def test_auth_context_is_immutable():
    with pytest.raises(Exception):
        ctx(R.EMPLOYEE).user_id = "x"


def test_permission_denied_carries_permission():
    err = PermissionDenied(P.VIEW_SALARY)
    assert err.permission is P.VIEW_SALARY and str(err) == "VIEW_SALARY"


def test_unknown_fields_are_rejected_not_silently_ignored():
    with pytest.raises(Exception):
        AuthContext(user_id="u", tenant_id="acme", role=R.HR_ADMIN)
