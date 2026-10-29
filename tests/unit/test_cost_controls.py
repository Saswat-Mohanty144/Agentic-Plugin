"""Cost controls: prompt caching, usage accounting, tenant budgets, compact tool results, no-LLM PF path (piece H)."""

import json

import pytest

from hrms_plugin.agents.orchestrator import SUPER_AGENT_SYSTEM_PROMPT, AgenticOrchestrator
from hrms_plugin.agents.tools import AgenticToolRegistry
from hrms_plugin.ai.llm import (
    BaseLLMClient,
    ChatMessage,
    LLMConfig,
    LLMGateway,
    LLMNotConfigured,
    LLMProvider,
    LLMResponse,
)
from hrms_plugin.governance.egress_log import EgressLog
from hrms_plugin.governance.llm_policy import LlmBudgetExceeded
from hrms_plugin.security.rbac import AuthContext, UserRole
from hrms_plugin.security.tenant_registry import LlmPolicy
from hrms_plugin.storage.memory_store import MemoryKeyValueStore
from tests.support.auth import NOW
from tests.support.scripted_llm import final, tool_call
from tests.unit.test_llm_providers import Wire, anthropic_client, anthropic_reply

ADMIN = AuthContext(user_id="t", tenant_id="acme", roles=frozenset({UserRole.SUPER_ADMIN}))
DAY = 86400
MIDNIGHT = NOW - NOW % DAY  # start of NOW's UTC day


# H1 — prompt caching on the Anthropic wire ---------------------------------------------------------------


async def test_first_system_block_is_the_cache_breakpoint_and_tail_is_auto_cached():
    wire = Wire(anthropic_reply([{"type": "text", "text": "hi"}]))
    await anthropic_client(wire).chat(
        [
            ChatMessage(role="system", content="frozen"),
            ChatMessage(role="system", content="per-request"),
            ChatMessage(role="user", content="q"),
        ]
    )
    body = wire.requests[0]["body"]
    assert body["system"] == [
        {"type": "text", "text": "frozen", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "per-request"},
    ]
    assert body["cache_control"] == {"type": "ephemeral"}


async def test_no_system_still_auto_caches_the_tail():
    wire = Wire(anthropic_reply([{"type": "text", "text": "hi"}]))
    await anthropic_client(wire).chat([ChatMessage(role="user", content="q")])
    body = wire.requests[0]["body"]
    assert "system" not in body and body["cache_control"] == {"type": "ephemeral"}


async def test_orchestrator_keeps_the_frozen_prompt_separate_from_request_data(scripted):
    gateway, client = scripted(final("ok"))
    await AgenticOrchestrator(llm_gateway=gateway, tool_registry=AgenticToolRegistry()).run(
        "q", jurisdiction="AE", context={"employee_id": "E-1"}, auth=ADMIN
    )
    first, second = client.calls[0]["messages"][:2]
    assert first.role == second.role == "system"
    assert first.content == SUPER_AGENT_SYSTEM_PROMPT  # byte-identical for every tenant and request
    assert "AE" in second.content and "acme" in second.content and "employee_id" in second.content


async def test_frozen_prefix_is_identical_across_tenants_and_jurisdictions(scripted):
    seen = []
    for tenant, jurisdiction in (("acme", "IN"), ("globex", "SA")):
        gateway, client = scripted(final("ok"))
        auth = AuthContext(user_id="u", tenant_id=tenant, roles=frozenset({UserRole.SUPER_ADMIN}))
        await AgenticOrchestrator(llm_gateway=gateway, tool_registry=AgenticToolRegistry()).run(
            "q", jurisdiction=jurisdiction, auth=auth
        )
        call = client.calls[0]
        seen.append((call["messages"][0].content, [t.name for t in call["tools"]]))
    assert seen[0] == seen[1]


# H2 — deterministic tool order -----------------------------------------------------------------------------


def test_tools_are_sorted_by_name():
    names = [d.name for d in AgenticToolRegistry().definitions_for(ADMIN)]
    assert names == sorted(names) and len(names) > 1


# H3 — usage accounting incl. cache fields ------------------------------------------------------------------


async def test_anthropic_usage_includes_cache_fields():
    reply = anthropic_reply([{"type": "text", "text": "x"}])
    reply["usage"] = {
        "input_tokens": 7, "output_tokens": 3, "cache_creation_input_tokens": 600, "cache_read_input_tokens": 1200
    }
    resp = await anthropic_client(Wire(reply)).chat([ChatMessage(role="user", content="q")])
    assert resp.usage == {
        "input_tokens": 7, "output_tokens": 3, "cache_creation_input_tokens": 600, "cache_read_input_tokens": 1200
    }


async def test_anthropic_stream_reports_usage_first():
    # usage leads the stream so a consumer that stops after the first token is still accounted (agy H-F3)
    wire = Wire(anthropic_reply([{"type": "text", "text": "x"}]))
    events = [e async for e in anthropic_client(wire).chat_stream([ChatMessage(role="user", content="q")])]
    assert [e["event"] for e in events] == ["usage", "token", "done"]
    assert events[0]["data"]["input_tokens"] == 7 and events[0]["data"]["output_tokens"] == 3


class Client(BaseLLMClient):
    def __init__(self, usage=None, stream_usage=True):
        super().__init__(LLMConfig(provider=LLMProvider.FAKE))
        self.usage = usage if usage is not None else {"input_tokens": 10, "output_tokens": 5}
        self.stream_usage = stream_usage
        self.calls = []

    async def chat(self, messages, tools=None, temperature=None):
        self.calls.append("chat")
        return LLMResponse(content="answer text", usage=dict(self.usage))

    async def chat_stream(self, messages, tools=None, temperature=None):
        self.calls.append("stream")
        yield {"event": "token", "data": "answer text"}
        usage = {"usage": self.usage} if self.stream_usage else {}
        yield {"event": "done", "data": {"finish_reason": "stop", **usage}}


def gateway(budget=None, log=None, client=None):
    gw = LLMGateway(
        config=LLMConfig(provider=LLMProvider.FAKE),
        egress_log=log,
        policy_resolver=lambda tenant, provider: LlmPolicy(allowed_providers=("fake",), daily_token_budget=budget),
    )
    gw.client = client or Client()
    return gw


def new_log():
    return EgressLog(MemoryKeyValueStore(), clock=lambda: NOW)


async def test_egress_records_cache_tokens():
    log = new_log()
    usage = {"input_tokens": 1, "output_tokens": 2, "cache_creation_input_tokens": 3, "cache_read_input_tokens": 4}
    await gateway(log=log, client=Client(usage=usage)).chat([ChatMessage(role="user", content="q")], tenant_id="acme")
    [row] = log.list("acme")
    assert (row["input_tokens"], row["output_tokens"], row["cache_creation_input_tokens"],
            row["cache_read_input_tokens"]) == (1, 2, 3, 4)


async def test_stream_usage_is_recorded():
    log = new_log()
    gw = gateway(log=log)
    [e async for e in gw.chat_stream([ChatMessage(role="user", content="q")], tenant_id="acme")]
    [row] = log.list("acme")
    assert (row["status"], row["input_tokens"], row["output_tokens"]) == ("COMPLETED", 10, 5)


# H4 — per-tenant daily budget ------------------------------------------------------------------------------


def seed(log, tenant, at, **tokens):
    clock_log = EgressLog(log._store, clock=lambda: at)
    key = clock_log.record(tenant_id=tenant, provider="fake", model="m", payload_sha256="sha256:x", redacted_kinds={})
    log.update(key, status="COMPLETED", **tokens)


def test_budget_must_be_positive_integer():
    with pytest.raises(ValueError):
        LlmPolicy(allowed_providers=("fake",), daily_token_budget=0)
    with pytest.raises(ValueError):
        LlmPolicy(allowed_providers=("fake",), daily_token_budget="100")
    assert LlmPolicy(allowed_providers=("fake",)).daily_token_budget is None


def test_tokens_today_counts_all_fields_for_this_tenant_and_utc_day_only():
    log = new_log()
    seed(log, "acme", MIDNIGHT, input_tokens=1, output_tokens=2, cache_creation_input_tokens=3,
         cache_read_input_tokens=4)
    seed(log, "acme", NOW, input_tokens=10)
    seed(log, "acme", MIDNIGHT - 0.001, input_tokens=1000)  # yesterday
    seed(log, "acme2", NOW, input_tokens=1000)  # other tenant sharing a prefix
    seed(log, "globex", NOW, input_tokens=1000)
    seed(log, "acme", NOW)  # in flight / failed: no usage reported
    assert log.tokens_today("acme") == 20


def test_tokens_today_is_not_limited_by_listing_cap():
    log = new_log()
    for _ in range(250):
        seed(log, "acme", NOW, input_tokens=1)
    assert log.tokens_today("acme") == 250


async def test_call_below_budget_is_sent():
    log = new_log()
    seed(log, "acme", NOW, input_tokens=99)
    gw = gateway(budget=100, log=log)
    await gw.chat([ChatMessage(role="user", content="q")], tenant_id="acme")
    assert gw.client.calls == ["chat"]


async def test_call_at_budget_is_refused_before_anything_is_sent():
    log = new_log()
    seed(log, "acme", NOW, input_tokens=60, output_tokens=40)
    gw = gateway(budget=100, log=log)
    with pytest.raises(LlmBudgetExceeded):
        await gw.chat([ChatMessage(role="user", content="q")], tenant_id="acme")
    assert gw.client.calls == [] and len(log.list("acme")) == 1


async def test_stream_is_refused_at_budget():
    log = new_log()
    seed(log, "acme", NOW, input_tokens=100)
    gw = gateway(budget=100, log=log)
    with pytest.raises(LlmBudgetExceeded):
        [e async for e in gw.chat_stream([ChatMessage(role="user", content="q")], tenant_id="acme")]
    assert gw.client.calls == []


async def test_no_budget_means_no_cap():
    log = new_log()
    seed(log, "acme", NOW, input_tokens=10**9)
    gw = gateway(budget=None, log=log)
    await gw.chat([ChatMessage(role="user", content="q")], tenant_id="acme")
    assert gw.client.calls == ["chat"]


async def test_budget_without_egress_log_fails_closed():
    gw = gateway(budget=100, log=None)
    with pytest.raises(LLMNotConfigured):
        await gw.chat([ChatMessage(role="user", content="q")], tenant_id="acme")
    assert gw.client.calls == []


async def test_budgeted_tenant_never_streams_without_usage():
    log = new_log()
    gw = gateway(budget=1000, log=log, client=Client(stream_usage=False))
    events = [e async for e in gw.chat_stream([ChatMessage(role="user", content="q")], tenant_id="acme")]
    assert gw.client.calls == ["chat"]
    assert "".join(e["data"] for e in events if e["event"] == "token") == "answer text"
    assert events[-1]["event"] == "done"
    assert log.list("acme")[0]["input_tokens"] == 10


def test_http_chat_over_budget_is_429(make_client, bearer, monkeypatch):
    from hrms_plugin.agents.supervisor import SupervisorAgent

    agent = SupervisorAgent()

    async def over_budget(**_kw):
        raise LlmBudgetExceeded("acme")

    monkeypatch.setattr(agent, "process_agentic", over_budget)
    res = make_client(supervisor_agent=agent).post(
        "/v1/chat", headers=bearer(), json={"message": "hi", "jurisdiction": "IN"}
    )
    assert res.status_code == 429 and res.json() == {"error": "LLM_BUDGET_EXHAUSTED"}


def test_http_stream_over_budget_is_429_before_streaming(make_client, bearer, monkeypatch):
    from hrms_plugin.agents.supervisor import SupervisorAgent

    agent = SupervisorAgent()

    def refuse(_tenant):
        raise LlmBudgetExceeded("acme")

    monkeypatch.setattr(agent.get_orchestrator().llm_gateway, "preflight", refuse)
    res = make_client(supervisor_agent=agent).post(
        "/v1/chat/stream", headers=bearer(), json={"message": "hi", "jurisdiction": "IN"}
    )
    assert res.status_code == 429 and res.json() == {"error": "LLM_BUDGET_EXHAUSTED"}


# H5 — compact, bounded tool results ------------------------------------------------------------------------


async def test_tool_results_are_compact_json(scripted):
    gateway_, client = scripted(tool_call("search_labor_regulations", query="gratuity"), final("ok"))
    await AgenticOrchestrator(llm_gateway=gateway_, tool_registry=AgenticToolRegistry()).run(
        "q", jurisdiction="AE", auth=ADMIN
    )
    tool_msg = client.calls[1]["messages"][-1]
    assert tool_msg.role == "tool"
    assert ", " not in tool_msg.content[:200] and '": ' not in tool_msg.content
    json.loads(tool_msg.content)


async def _run_with_tool_output(scripted, output):
    registry = AgenticToolRegistry()

    async def handler(**_kw):
        return output

    registry._handlers["search_labor_regulations"] = handler
    gateway_, client = scripted(tool_call("search_labor_regulations", query="x"), final("ok"))
    resp = await AgenticOrchestrator(llm_gateway=gateway_, tool_registry=registry).run(
        "q", jurisdiction="AE", auth=ADMIN
    )
    return resp, client.calls[1]["messages"][-1].content


async def test_oversized_tool_output_is_replaced_for_the_llm_only(scripted):
    big = {"rows": ["x" * 100] * 300}
    resp, sent = await _run_with_tool_output(scripted, big)
    assert json.loads(sent) == {"error": "TOOL_OUTPUT_TOO_LARGE", "chars": len(json.dumps(big, separators=(",", ":")))}
    assert resp.structured_data == big


async def test_tool_output_at_the_limit_is_sent_whole(scripted):
    # {"v":"..."} is 8 chars of framing
    output = {"v": "y" * (20000 - 8)}
    _, sent = await _run_with_tool_output(scripted, output)
    assert len(sent) == 20000 and json.loads(sent) == output


async def test_tool_output_one_over_the_limit_is_replaced(scripted):
    output = {"v": "y" * (20000 - 7)}
    _, sent = await _run_with_tool_output(scripted, output)
    assert json.loads(sent)["error"] == "TOOL_OUTPUT_TOO_LARGE"


# H6 — no-LLM PF calculation endpoint ------------------------------------------------------------------------


@pytest.fixture
def pf_app(make_client, monkeypatch, request):
    from hrms_plugin.server import app as app_module

    calls = []

    class Svc:
        async def calculate_pf(self, tenant_id, employee_id, period, jurisdiction):
            calls.append((tenant_id, employee_id, period, jurisdiction))
            return getattr(request, "param", {"employee_contribution": "1680"})

    monkeypatch.setattr(app_module, "payroll_service", Svc())
    return make_client(), calls


def test_pf_endpoint_uses_token_tenant_and_no_llm(pf_app, bearer):
    client, calls = pf_app
    res = client.post(
        "/v1/payroll/pf/calculate",
        headers=bearer(roles=["payroll_officer"]),
        json={"employee_id": "HR-EMP-00001", "period": "2026-09"},
    )
    assert res.status_code == 200 and res.json()["employee_contribution"] == "1680"
    assert calls == [("acme", "HR-EMP-00001", "2026-09", "IN")]


def test_pf_endpoint_requires_view_salary(pf_app, bearer):
    client, calls = pf_app
    res = client.post(
        "/v1/payroll/pf/calculate", headers=bearer(roles=["staff"]),
        json={"employee_id": "HR-EMP-00001", "period": "2026-09"},
    )
    assert res.status_code == 403 and calls == []


@pytest.mark.parametrize(
    "body",
    [
        {"employee_id": "HR-EMP-00001"},
        {"period": "2026-09"},
        {"employee_id": "HR-EMP-00001", "period": "2026-09", "tenant_id": "globex"},
        {"employee_id": "HR-EMP-00001", "period": "2026-09", "jurisdiction": "AE"},
        {"employee_id": 1, "period": "2026-09"},
    ],
)
def test_pf_endpoint_rejects_bad_bodies(pf_app, bearer, body):
    client, calls = pf_app
    res = client.post("/v1/payroll/pf/calculate", headers=bearer(roles=["payroll_officer"]), json=body)
    assert res.status_code == 422 and calls == []


@pytest.mark.parametrize(
    "pf_app,status",
    [
        ({"error": "INVALID_ARGUMENT", "detail": "period must be YYYY-MM"}, 422),
        ({"error": "HOST_NOT_CONFIGURED"}, 409),
        ({"error": "RULES_NOT_CONFIGURED"}, 409),
        ({"error": "MAPPING_NOT_APPROVED"}, 409),
        ({"error": "SCHEMA_DRIFT"}, 409),
        ({"error": "NOT_APPLICABLE"}, 422),
        ({"error": "SLIP_NOT_FOUND"}, 404),
        ({"error": "AMBIGUOUS_SLIPS"}, 409),
        ({"error": "PF_COMPONENT_MISSING", "detail": "x"}, 409),
        ({"error": "HOST_ERROR"}, 502),
        ({"error": "INCONSISTENT_PAYLOAD"}, 502),
        ({"error": "SOMETHING_NEW"}, 502),
    ],
    indirect=["pf_app"],
)
def test_pf_endpoint_maps_service_errors(pf_app, bearer, status):
    client, _ = pf_app
    res = client.post(
        "/v1/payroll/pf/calculate", headers=bearer(roles=["payroll_officer"]),
        json={"employee_id": "HR-EMP-00001", "period": "2026-09"},
    )
    assert res.status_code == status and res.json()["error"]


async def test_gemini_keeps_every_system_message():
    import httpx

    from hrms_plugin.ai.llm import GeminiClient
    from tests.unit.test_llm_providers import Sleeps

    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        reply = {"candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}]}
        return httpx.Response(200, json=reply)

    config = LLMConfig(provider=LLMProvider.GEMINI, model="gemini-2.5-flash", api_key="k")
    client = GeminiClient(config, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), sleep=Sleeps())
    await client.chat([ChatMessage(role="system", content="frozen"), ChatMessage(role="system", content="dynamic"),
                       ChatMessage(role="user", content="q")])
    assert seen[0]["system_instruction"]["parts"] == [{"text": "frozen"}, {"text": "dynamic"}]
