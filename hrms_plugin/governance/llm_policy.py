"""Per-tenant LLM policy enforcement, fail closed (piece G, decision G2)."""

from __future__ import annotations

from typing import Callable, Optional

from hrms_plugin.security.tenant_registry import LlmPolicy, TenantRegistry, load_registry_from_env


class LlmNotAllowed(Exception):
    """This tenant may not send data to the configured LLM provider."""


class LlmBudgetExceeded(Exception):
    """This tenant has used its daily LLM token budget (piece H4)."""


PolicyResolver = Callable[[str, str], LlmPolicy]


def registry_policy_resolver(registry: TenantRegistry) -> PolicyResolver:
    def resolve(tenant_id: str, provider: str) -> LlmPolicy:
        tenant = next((t for t in registry.tenants if t.tenant_id == tenant_id), None)
        policy: Optional[LlmPolicy] = tenant.llm_policy if tenant else None
        if policy is None or provider not in policy.allowed_providers:
            raise LlmNotAllowed(f"tenant {tenant_id!r} may not use LLM provider {provider!r}")
        return policy

    return resolve


def env_policy_resolver() -> PolicyResolver:
    def resolve(tenant_id: str, provider: str) -> LlmPolicy:
        return registry_policy_resolver(load_registry_from_env())(tenant_id, provider)

    return resolve
