"""QE-3 (Q4): per-tenant request rate limit on LLM- and voice-backed routes (429 RATE_LIMITED + Retry-After).

The limit is a tenant policy value (owner input); absent means no limit, the same rule as the daily token budget.
"""

import pytest
from pydantic import ValidationError

from hrms_plugin.governance.rate_limit import TenantRateLimiter
from hrms_plugin.security.tenant_registry import LlmPolicy


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


# policy ------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("value", [0, -1, True, 1.5, "10"])
def test_invalid_limits_rejected(value):
    with pytest.raises(ValidationError):
        LlmPolicy(allowed_providers=("fake",), requests_per_minute=value)


def test_limit_defaults_to_none():
    assert LlmPolicy(allowed_providers=("fake",)).requests_per_minute is None


# limiter -----------------------------------------------------------------------------------------------------
def test_burst_up_to_the_limit_then_refuse_then_refill():
    clock = Clock()
    limiter = TenantRateLimiter(lambda tenant: 3, clock=clock)
    assert [limiter.check("acme") for _ in range(3)] == [None, None, None]
    retry = limiter.check("acme")
    assert retry == pytest.approx(20.0)  # one request per 20 s at 3/min
    clock.now += 20
    assert limiter.check("acme") is None and limiter.check("acme") is not None


def test_tenants_are_isolated_and_unlimited_tenants_pass():
    clock = Clock()
    limiter = TenantRateLimiter(lambda tenant: 1 if tenant == "acme" else None, clock=clock)
    assert limiter.check("acme") is None and limiter.check("acme") is not None
    assert all(limiter.check("globex") is None for _ in range(100))


def test_bucket_never_exceeds_capacity_after_idle():
    clock = Clock()
    limiter = TenantRateLimiter(lambda tenant: 2, clock=clock)
    limiter.check("acme")
    clock.now += 3600
    assert [limiter.check("acme") for _ in range(3)][2] is not None


# routes ------------------------------------------------------------------------------------------------------
LLM_ROUTES = [("/v1/chat", {"message": "hi", "jurisdiction": "IN", "agentic": False}),
              ("/api/compliance/agent/chat", {"message": "hi", "jurisdiction": "IN"}),
              ("/v1/voice/synthesize", {"text": "hello"})]


@pytest.mark.parametrize("route, body", LLM_ROUTES)
def test_llm_routes_are_rate_limited(make_client, bearer, route, body):
    client = make_client(raise_server_exceptions=False)
    client.app.state.rate_limiter = TenantRateLimiter(lambda tenant: 1, clock=Clock())
    first = client.post(route, json=body, headers=bearer(roles=["staff"]))
    second = client.post(route, json=body, headers=bearer(roles=["staff"]))
    assert first.status_code != 429
    assert second.status_code == 429 and second.json() == {"error": "RATE_LIMITED"}
    assert second.headers["retry-after"] == "60"


def test_unauthenticated_is_401_not_429(make_client):
    client = make_client()
    client.app.state.rate_limiter = TenantRateLimiter(lambda tenant: 1, clock=Clock())
    assert all(client.post("/v1/chat", json={"message": "hi"}).status_code == 401 for _ in range(3))


def test_non_llm_routes_are_not_limited(make_client, bearer):
    client = make_client()
    client.app.state.rate_limiter = TenantRateLimiter(lambda tenant: 1, clock=Clock())
    assert all(client.get("/v1/governance/llm-egress", headers=bearer(roles=["hr_manager"])).status_code == 200
               for _ in range(3))


def test_default_app_reads_the_limit_from_the_tenant_policy(make_client, bearer):
    client = make_client()
    assert client.app.state.rate_limiter.limit_for("acme") is None  # fixture tenant sets no limit
