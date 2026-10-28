"""Per-tenant token issuer registry, loaded from YAML and validated strictly at startup."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Literal, Mapping, Optional, Tuple, Union
from urllib.parse import urlsplit

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from hrms_plugin.security.rbac import UserRole

REGISTRY_ENV_VAR = "HRMS_TENANT_REGISTRY"


class RegistryError(Exception):
    """The tenant registry is missing or invalid; the service must not start."""


class ClaimNames(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant: StrictStr = Field(min_length=1)
    roles: StrictStr = Field(min_length=1)
    subject: StrictStr = Field(min_length=1)


class LlmPolicy(BaseModel):
    """Which LLM providers may receive this tenant's (redacted) data; a legal decision per customer (piece G2)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed_providers: Tuple[
        Literal["fake", "openai", "groq", "openrouter", "anthropic", "gemini", "nvidia"], ...
    ] = Field(min_length=1)
    tokenize_compensation: StrictBool = False


class TenantConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: StrictStr = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    issuer: StrictStr = Field(min_length=1)
    jwks_url: StrictStr
    audience: StrictStr = Field(min_length=1)
    algorithms: Tuple[Literal["RS256", "ES256"], ...] = Field(min_length=1)
    claims: ClaimNames
    role_map: Dict[StrictStr, UserRole] = Field(min_length=1)
    allowed_origins: Tuple[StrictStr, ...]
    leeway_seconds: StrictInt = Field(ge=0, le=120)
    max_token_lifetime_seconds: StrictInt = Field(ge=60, le=86400)
    llm_policy: Optional[LlmPolicy] = None  # absent → no LLM calls for this tenant (fail closed)

    @field_validator("jwks_url")
    @classmethod
    def _https_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("jwks_url must be an absolute https:// URL")
        return value

    @field_validator("allowed_origins")
    @classmethod
    def _exact_origins(cls, values: Tuple[str, ...]) -> Tuple[str, ...]:
        for origin in values:
            parts = urlsplit(origin)
            try:
                parts.port
            except ValueError:
                raise ValueError(f"invalid port in origin {origin!r}") from None
            if (
                parts.scheme != "https"
                or not parts.hostname
                or parts.path
                or parts.query
                or parts.fragment
                or parts.username
            ):
                raise ValueError(f"invalid origin {origin!r}: must be https://host[:port] with nothing after it")
        return values


class TenantRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenants: Tuple[TenantConfig, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> "TenantRegistry":
        seen = set()
        claims_by_issuer: Dict[str, ClaimNames] = {}
        for tenant in self.tenants:
            if tenant.tenant_id in seen:
                raise ValueError(f"duplicate tenant_id {tenant.tenant_id!r}")
            seen.add(tenant.tenant_id)
            first = claims_by_issuer.setdefault(tenant.issuer, tenant.claims)
            if first != tenant.claims:
                raise ValueError(f"tenants sharing issuer {tenant.issuer!r} must use identical claims")
        return self

    def claims_for_issuer(self, issuer: str) -> Optional[ClaimNames]:
        for tenant in self.tenants:
            if tenant.issuer == issuer:
                return tenant.claims
        return None

    def find(self, issuer: str, tenant_id: str) -> Optional[TenantConfig]:
        for tenant in self.tenants:
            if tenant.issuer == issuer and tenant.tenant_id == tenant_id:
                return tenant
        return None

    def all_origins(self) -> List[str]:
        return sorted({origin for tenant in self.tenants for origin in tenant.allowed_origins})


def load_registry(path: Union[str, Path]) -> TenantRegistry:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise RegistryError(f"cannot read tenant registry {path}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RegistryError(f"invalid YAML in tenant registry {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RegistryError(f"tenant registry {path} must be a mapping with a 'tenants' list")
    try:
        return TenantRegistry.model_validate(data)
    except ValidationError as exc:
        raise RegistryError(f"invalid tenant registry {path}: {exc}") from exc


def load_registry_from_env(env: Optional[Mapping[str, str]] = None) -> TenantRegistry:
    env = os.environ if env is None else env
    path = env.get(REGISTRY_ENV_VAR)
    if not path:
        raise RegistryError(f"{REGISTRY_ENV_VAR} is not set; the plugin cannot authenticate requests without it")
    return load_registry(path)
