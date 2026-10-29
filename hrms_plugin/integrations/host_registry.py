"""Per-tenant host HRMS connection registry (piece F1, decision F1-3). Secrets live in env vars, never in the file."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Literal, Mapping, Optional, Tuple, Union
from urllib.parse import urlsplit

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

HOSTS_ENV_VAR = "HRMS_HOSTS"


class HostRegistryError(Exception):
    """Host registry missing/invalid, or credentials unavailable."""


class HostConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: StrictStr = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    vendor: Literal["frappe", "iceipts"]
    base_url: StrictStr
    # frappe: `token key:secret`; iceipts: bearer token + the service account's user id (piece I6)
    api_key_env: Optional[StrictStr] = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,63}$")
    api_secret_env: Optional[StrictStr] = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,63}$")
    token_env: Optional[StrictStr] = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,63}$")
    service_user_id: Optional[StrictStr] = Field(
        default=None, pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    )

    @field_validator("service_user_id")
    @classmethod
    def _lower_uuid(cls, value: Optional[str]) -> Optional[str]:
        return value.lower() if value else value  # RFC 4122: hex is case-insensitive (agy I-F9)
    timeout_seconds: StrictInt = Field(ge=1, le=60)
    # Per-tenant write-back allowlist (piece F2, W3): doctype -> field -> value type. Empty means no writes.
    writable_fields: Dict[StrictStr, Dict[StrictStr, Literal["int", "date", "string", "decimal"]]] = Field(
        default_factory=dict
    )

    @field_validator("writable_fields")
    @classmethod
    def _no_pii_writes(cls, value: Dict[str, Dict[str, str]]) -> Dict[str, Dict[str, str]]:
        from hrms_plugin.security.pii import identifier_kind

        for doctype, fields in value.items():
            for field in fields:
                if identifier_kind(field):
                    raise ValueError(f"{doctype}.{field} is a personal identifier and cannot be writable")
        return value

    @field_validator("base_url")
    @classmethod
    def _https(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.hostname or parts.query or parts.fragment:
            raise ValueError("base_url must be an https:// URL without query or fragment")
        if parts.username or parts.password:
            raise ValueError("base_url must not embed credentials; use api_key_env / api_secret_env")
        return value.rstrip("/")

    @model_validator(mode="after")
    def _vendor_fields(self) -> "HostConfig":
        frappe = (self.api_key_env, self.api_secret_env)
        iceipts = (self.token_env, self.service_user_id)
        if self.vendor == "frappe":
            if not all(frappe) or any(iceipts):
                raise ValueError("frappe hosts need api_key_env and api_secret_env only")
        elif not all(iceipts) or any(frappe) or self.writable_fields:
            raise ValueError("iceipts hosts need token_env and service_user_id, and support no write-back")
        return self

    def credentials(self, env: Optional[Mapping[str, str]] = None) -> Tuple[str, str]:
        """Frappe API key and secret."""
        if self.vendor != "frappe":
            raise HostRegistryError(f"tenant {self.tenant_id!r} is not a frappe host")
        env = os.environ if env is None else env
        key, secret = env.get(self.api_key_env), env.get(self.api_secret_env)
        if not key or not secret:
            raise HostRegistryError(f"credentials for tenant {self.tenant_id!r} are not set in the environment")
        return key, secret

    def bearer_token(self, env: Optional[Mapping[str, str]] = None) -> str:
        """iCeipts service-account bearer token."""
        if self.vendor != "iceipts":
            raise HostRegistryError(f"tenant {self.tenant_id!r} is not an iceipts host")
        env = os.environ if env is None else env
        token = env.get(self.token_env) if self.token_env else None
        if not token:
            raise HostRegistryError(f"token for tenant {self.tenant_id!r} is not set in the environment")
        return token


class HostRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenants: Tuple[HostConfig, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> "HostRegistry":
        ids = [t.tenant_id for t in self.tenants]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate tenant_id in host registry")
        return self

    def for_tenant(self, tenant_id: str) -> Optional[HostConfig]:
        return next((t for t in self.tenants if t.tenant_id == tenant_id), None)


def load_host_registry(path: Union[str, Path]) -> HostRegistry:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return HostRegistry.model_validate(data)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise HostRegistryError(f"invalid host registry {path}: {exc}") from exc


def load_host_registry_from_env(env: Optional[Mapping[str, str]] = None) -> HostRegistry:
    env = os.environ if env is None else env
    path = env.get(HOSTS_ENV_VAR)
    if not path:
        raise HostRegistryError(f"{HOSTS_ENV_VAR} is not set")
    return load_host_registry(path)
