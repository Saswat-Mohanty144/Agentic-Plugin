"""Zero-Trust Security, PII Governance, and Audit Attributions."""

from hrms_plugin.security.audit import (
    AuditSigningError,
    HostAuditGateway,
    HostAuditHeaders,
    build_audit_signed_message,
    verify_audit_headers,
)
from hrms_plugin.security.pii import PiiEgressBlocked, PiiSession
from hrms_plugin.security.rbac import (
    AuthContext,
    Permission,
    RbacPolicyEnforcer,
    UserRole,
)

__all__ = [
    "AuditSigningError",
    "HostAuditGateway",
    "HostAuditHeaders",
    "PiiEgressBlocked",
    "PiiSession",
    "AuthContext",
    "Permission",
    "RbacPolicyEnforcer",
    "UserRole",
    "build_audit_signed_message",
    "verify_audit_headers",
]
