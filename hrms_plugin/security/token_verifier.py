"""Verifies host-signed JWTs against the tenant registry and produces an AuthContext."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, FrozenSet, Optional, Sequence

import jwt

from hrms_plugin.security.jwks_cache import JwksCache, UnknownSigningKey
from hrms_plugin.security.rbac import AuthContext, UserRole
from hrms_plugin.security.tenant_registry import TenantConfig, TenantRegistry

logger = logging.getLogger("hrms_plugin.security.auth")

MAX_TOKEN_BYTES = 8192
MAX_ROLES = 50
MAX_SUBJECT_LENGTH = 256

_JWT_ERROR_REASONS = (
    (jwt.InvalidSignatureError, "BAD_SIGNATURE"),
    (jwt.InvalidAudienceError, "BAD_AUDIENCE"),
    (jwt.InvalidIssuerError, "BAD_ISSUER"),
    (jwt.MissingRequiredClaimError, "MISSING_CLAIM"),
    (jwt.InvalidAlgorithmError, "ALG_NOT_ALLOWED"),
    (jwt.exceptions.InvalidSubjectError, "SUBJECT_INVALID"),
    (jwt.DecodeError, "MALFORMED"),
)


class AuthError(Exception):
    """Token rejected. `reason` is logged; it is never returned to the caller."""

    def __init__(self, reason: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(reason)
        self.reason = reason
        self.details = dict(details or {})


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class TokenVerifier:
    def __init__(self, registry: TenantRegistry, jwks_cache: JwksCache, clock: Callable[[], float] = time.time):
        self._registry = registry
        self._jwks = jwks_cache
        self._clock = clock

    async def verify(self, authorization_headers: Sequence[str]) -> AuthContext:
        token = self._extract_token(authorization_headers)
        try:
            header = jwt.get_unverified_header(token)
            unverified = jwt.decode(token, options={"verify_signature": False})
        except jwt.PyJWTError:
            raise AuthError("MALFORMED") from None

        details: Dict[str, Any] = {}
        issuer = unverified.get("iss")
        claim_names = self._registry.claims_for_issuer(issuer) if isinstance(issuer, str) else None
        if claim_names is None:
            raise AuthError("UNKNOWN_ISSUER")
        details["iss"] = issuer

        tenant_claim = unverified.get(claim_names.tenant)
        tenant = self._registry.find(issuer, tenant_claim) if isinstance(tenant_claim, str) else None
        if tenant is None:
            raise AuthError("UNKNOWN_TENANT", details)
        details["tenant"] = tenant.tenant_id

        alg = header.get("alg")
        if alg not in tenant.algorithms:
            raise AuthError("ALG_NOT_ALLOWED", details)
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise AuthError("KID_MISSING", details)
        try:
            key = await self._jwks.get_signing_key(tenant.jwks_url, kid)
        except UnknownSigningKey:
            raise AuthError("UNKNOWN_KID", details) from None

        try:
            payload = jwt.decode(
                token,
                key=key,
                algorithms=[alg],
                audience=tenant.audience,
                issuer=tenant.issuer,
                options={"verify_exp": False, "verify_nbf": False, "verify_iat": False, "require": ["exp", "iat"]},
            )
        except jwt.PyJWTError as exc:
            raise AuthError(self._reason_for(exc), details) from None

        details["sub"] = payload.get(claim_names.subject)
        details["jti"] = payload.get("jti")
        self._check_times(payload, tenant, details)

        subject = payload.get(claim_names.subject)
        if not isinstance(subject, str) or not subject or len(subject) > MAX_SUBJECT_LENGTH:
            raise AuthError("SUBJECT_INVALID", details)
        roles = self._map_roles(payload.get(claim_names.roles), tenant, details)
        jti = payload.get("jti")
        return AuthContext(
            user_id=subject,
            tenant_id=tenant.tenant_id,
            roles=roles,
            issued_at=float(payload["iat"]),
            token_id=jti if isinstance(jti, str) else None,
        )

    @staticmethod
    def _extract_token(headers: Sequence[str]) -> str:
        if len(headers) != 1:
            raise AuthError("HEADER_COUNT")
        scheme, separator, token = headers[0].lstrip().partition(" ")
        if scheme.lower() != "bearer" or not separator:
            raise AuthError("SCHEME")
        token = token.strip()
        if not token:
            raise AuthError("EMPTY_TOKEN")
        if len(token.encode("utf-8")) > MAX_TOKEN_BYTES:
            raise AuthError("TOKEN_TOO_LARGE")
        return token

    @staticmethod
    def _reason_for(exc: jwt.PyJWTError) -> str:
        for exc_type, reason in _JWT_ERROR_REASONS:
            if isinstance(exc, exc_type):
                return reason
        return "INVALID_TOKEN"

    def _check_times(self, payload: Dict[str, Any], tenant: TenantConfig, details: Dict[str, Any]) -> None:
        now = self._clock()
        leeway = tenant.leeway_seconds
        exp, iat = payload["exp"], payload["iat"]
        if not _is_number(exp) or not _is_number(iat):
            raise AuthError("TIME_CLAIM_INVALID", details)
        if now > exp + leeway:
            raise AuthError("EXPIRED", details)
        if iat > now + leeway:
            raise AuthError("IAT_IN_FUTURE", details)
        if "nbf" in payload:
            nbf = payload["nbf"]
            if not _is_number(nbf):
                raise AuthError("TIME_CLAIM_INVALID", details)
            if nbf > now + leeway:
                raise AuthError("NOT_YET_VALID", details)
        if exp - iat > tenant.max_token_lifetime_seconds:
            raise AuthError("LIFETIME_TOO_LONG", details)

    @staticmethod
    def _map_roles(raw: Any, tenant: TenantConfig, details: Dict[str, Any]) -> FrozenSet[UserRole]:
        if not isinstance(raw, list) or len(raw) > MAX_ROLES or not all(isinstance(r, str) for r in raw):
            raise AuthError("ROLES_INVALID", details)
        mapped = {tenant.role_map[r] for r in raw if r in tenant.role_map}
        unmapped = sorted({r for r in raw if r not in tenant.role_map})
        if unmapped:
            logger.info(
                "unmapped host roles ignored",
                extra={"reason": "UNMAPPED_ROLES", "tenant": tenant.tenant_id, "roles_unmapped": unmapped},
            )
        return frozenset(mapped)
