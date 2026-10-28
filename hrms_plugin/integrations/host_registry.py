"""Per-tenant host HRMS connection registry (piece F1, decision F1-3). Secrets live in env vars, never in the file."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Mapping, Optional, Tuple, Union
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError, field_validator

HOSTS_ENV_VAR = "HRMS_HOSTS"


class HostRegistryError(Exception):
    """Host registry missing/invalid, or credentials unavailable."""


class HostConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: StrictStr = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    vendor: Literal["frappe"]
    base_url: StrictStr
    api_key_env: StrictStr = Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")
    api_secret_env: StrictStr = Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")
    timeout_seconds: StrictInt = Field(ge=1, le=60)

    @field_validator("base_url")
    @classmethod
    def _https(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.hostname or parts.query or parts.fragment:
            raise ValueError("base_url must be an https:// URL without query or fragment")
        return value.rstrip("/")

    def credentials(self, env: Optional[Mapping[str, str]] = None) -> Tuple[str, str]:
        env = os.environ if env is None else env
        key, secret = env.get(self.api_key_env), env.get(self.api_secret_env)
        if not key or not secret:
            raise HostRegistryError(f"credentials for tenant {self.tenant_id!r} are not set in the environment")
        return key, secret


class HostRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenants: Tuple[HostConfig, ...] = Field(min_length=1)

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
