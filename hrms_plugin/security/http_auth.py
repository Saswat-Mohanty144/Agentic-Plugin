"""FastAPI dependencies that authenticate the caller and enforce a permission per route."""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from fastapi import Request

from hrms_plugin.security.jwks_cache import AuthUnavailable
from hrms_plugin.security.rbac import AuthContext, Permission, RbacPolicyEnforcer
from hrms_plugin.security.token_verifier import AuthError

logger = logging.getLogger("hrms_plugin.security.auth")

_CHALLENGE = {"WWW-Authenticate": 'Bearer error="invalid_token"'}
_enforcer = RbacPolicyEnforcer()


class ApiError(Exception):
    """An error rendered verbatim as the JSON response body."""

    def __init__(self, status_code: int, body: Dict[str, str], headers: Optional[Dict[str, str]] = None):
        super().__init__(body.get("error", ""))
        self.status_code = status_code
        self.body = body
        self.headers = headers


def _log_fields(auth: AuthContext, reason: str) -> Dict[str, Any]:
    return {"reason": reason, "tenant": auth.tenant_id, "sub": auth.user_id, "jti": auth.token_id}


async def authenticate(request: Request) -> AuthContext:
    verifier = request.app.state.token_verifier
    try:
        auth = await verifier.verify(request.headers.getlist("authorization"))
    except AuthError as exc:
        logger.warning("authentication rejected", extra={"reason": exc.reason, **exc.details})
        raise ApiError(401, {"error": "UNAUTHENTICATED"}, _CHALLENGE) from None
    except AuthUnavailable as exc:
        logger.error("authentication unavailable", extra={"reason": "AUTH_UNAVAILABLE", "jwks_url": exc.jwks_url})
        raise ApiError(503, {"error": "AUTH_UNAVAILABLE"}) from None

    header_tenant = request.headers.get("x-tenant-id")
    if header_tenant is not None and header_tenant != auth.tenant_id:
        logger.warning("tenant header mismatch", extra=_log_fields(auth, "TENANT_MISMATCH"))
        raise ApiError(403, {"error": "TENANT_MISMATCH"})
    return auth


def require(permission: Permission, *, max_token_age_seconds: Optional[int] = None) -> Callable[..., Any]:
    """Dependency factory: authenticate, check the permission, then (optionally) token freshness."""

    async def dependency(request: Request) -> AuthContext:
        auth = await authenticate(request)
        if not _enforcer.is_authorized(auth, permission):
            fields = {**_log_fields(auth, "FORBIDDEN"), "permission": permission.value}
            logger.warning("permission denied", extra=fields)
            raise ApiError(403, {"error": "FORBIDDEN", "permission": permission.value})
        if max_token_age_seconds is not None:
            age = request.app.state.clock() - (auth.issued_at or 0.0)
            if age > max_token_age_seconds:
                logger.warning("token too old for this action", extra=_log_fields(auth, "REAUTH_REQUIRED"))
                raise ApiError(401, {"error": "REAUTH_REQUIRED"}, _CHALLENGE)
        return auth

    dependency.permission = permission  # type: ignore[attr-defined]
    dependency.max_token_age_seconds = max_token_age_seconds  # type: ignore[attr-defined]
    return dependency
