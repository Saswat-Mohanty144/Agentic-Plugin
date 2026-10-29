"""Regression tests for the agy review of piece H (findings F1-F8)."""

import json

import pytest

from hrms_plugin.agents.orchestrator import AgenticOrchestrator, tool_result_content
from hrms_plugin.agents.tools import AgenticToolRegistry
from hrms_plugin.ai.llm import BaseLLMClient, ChatMessage, LLMConfig, LLMGateway, LLMProvider, LLMResponse, ToolCall
from hrms_plugin.governance.egress_log import EgressLog
from hrms_plugin.security.rbac import AuthContext, UserRole
from hrms_plugin.security.tenant_registry import LlmPolicy
from hrms_plugin.storage.memory_store import MemoryKeyValueStore
from tests.support.auth import NOW
from tests.unit.test_llm_providers import TOOL, Wire, anthropic_client, anthropic_reply

ADMIN = AuthContext(user_id="t", tenant_id="acme", roles=frozenset({UserRole.SUPER_ADMIN}))


def gateway(client, budget=None, log=None):
    gw = LLMGateway(
        config=LLMConfig(provider=LLMProvider.FAKE),
        egress_log=log,
        policy_resolver=lambda tenant, provider: LlmPolicy(
            allowed_providers=("fake",), max_data_tier=2, daily_token_budget=budget),
    )
    gw.client = client
    return gw


class Client(BaseLLMClient):
    """Records what it was sent; scripted replies; optional usage-first stream."""

    def __init__(self, replies=(), stream_reports_usage=False, usage=None):
        super().__init__(LLMConfig(provider=LLMProvider.FAKE))
        self.replies = list(replies)
        self.stream_reports_usage = stream_reports_usage
        self.usage = usage or {"input_tokens": 60, "output_tokens": 40}
        self.calls = []

    async def chat(self, messages, tools=None, temperature=None):
        self.calls.append({"kind": "chat", "tools": tools})
        reply = self.replies.pop(0) if self.replies else LLMResponse(content="answer")
        reply.usage = dict(self.usage)
        return reply

    async def chat_stream(self, messages, tools=None, temperature=None):
        self.calls.append({"kind": "stream", "tools": tools})
        yield {"event": "usage", "data": dict(self.usage)}
        yield {"event": "token", "data": "first "}
        yield {"event": "token", "data": "second"}
        yield {"event": "done", "data": {"finish_reason": "stop"}}


def new_log():
    return EgressLog(MemoryKeyValueStore(), clock=lambda: NOW)


# F1 — the synthesis stream keeps the cached tools prefix but can never call a tool -------------------------------


async def test_anthropic_text_only_stream_sends_tools_with_tool_choice_none():
    wire = Wire(anthropic_reply([{"type": "text", "text": "x"}]))
    gw = gateway(anthropic_client(wire))
    gw.client.stream_reports_usage = True
    [e async for e in gw.chat_stream([ChatMessage(role="user", content="q")], tools=[TOOL], tenant_id="acme")]
    body = wire.requests[0]["body"]
    assert [t["name"] for t in body["tools"]] == ["search_labor_regulations"]
    assert body["tool_choice"] == {"type": "none"}


async def test_anthropic_chat_without_tool_choice_sends_none_field():
    wire = Wire(anthropic_reply([{"type": "text", "text": "x"}]))
    await anthropic_client(wire).chat([ChatMessage(role="user", content="q")], tools=[TOOL])
    assert "tool_choice" not in wire.requests[0]["body"]


async def test_clients_without_tool_choice_get_no_tools_in_a_text_only_stream():
    client = Client()
    gw = gateway(client)
    [e async for e in gw.chat_stream([ChatMessage(role="user", content="q")], tools=[TOOL], tenant_id="acme")]
    assert client.calls == [{"kind": "stream", "tools": None}]


async def test_orchestrator_synthesis_passes_the_same_tools(scripted):
    from tests.support.scripted_llm import final, tool_call

    gw, client = scripted(tool_call("search_labor_regulations", query="a"), final("ok"))
    seen = {}
    original = gw.chat_stream

    def spy(messages, tools=None, **kw):
        seen["tools"] = tools
        return original(messages, tools=tools, **kw)

    gw.chat_stream = spy
    [e async for e in AgenticOrchestrator(llm_gateway=gw, tool_registry=AgenticToolRegistry()).run_stream(
        "q", jurisdiction="AE", auth=ADMIN)]
    assert [t.name for t in seen["tools"]] == [t.name for t in client.calls[0]["tools"]]


# F6 — budgeted fallback cannot drop tool calls because it never offers tools -------------------------------------


async def test_budgeted_fallback_never_offers_tools():
    client = Client(replies=[LLMResponse(content="text")])
    gw = gateway(client, budget=1000, log=new_log())
    [e async for e in gw.chat_stream([ChatMessage(role="user", content="q")], tools=[TOOL], tenant_id="acme")]
    assert client.calls == [{"kind": "chat", "tools": None}]


# F3 — an abandoned stream still accounts its usage ---------------------------------------------------------------


async def test_stream_abandoned_after_first_token_records_usage():
    log = new_log()
    gw = gateway(Client(stream_reports_usage=True), log=log)
    stream = gw.chat_stream([ChatMessage(role="user", content="q")], tenant_id="acme")
    async for event in stream:
        if event["event"] == "token":
            break
    await stream.aclose()
    [row] = log.list("acme")
    assert (row["status"], row["input_tokens"], row["output_tokens"]) == ("ABORTED", 60, 40)
    assert log.tokens_today("acme") == 100


async def test_usage_event_is_not_forwarded():
    gw = gateway(Client(stream_reports_usage=True), log=new_log())
    events = [e async for e in gw.chat_stream([ChatMessage(role="user", content="q")], tenant_id="acme")]
    assert [e["event"] for e in events] == ["token", "token", "done"]


async def test_stream_provider_error_marks_failed():
    class Boom(Client):
        async def chat_stream(self, messages, tools=None, temperature=None):
            yield {"event": "token", "data": "x"}
            raise RuntimeError("provider dropped")

    log = new_log()
    gw = gateway(Boom(), log=log)
    with pytest.raises(RuntimeError):
        [e async for e in gw.chat_stream([ChatMessage(role="user", content="q")], tenant_id="acme")]
    assert log.list("acme")[0]["status"] == "FAILED"


# F4 — budget check reads one counter, not every row -----------------------------------------------------------


class CountingStore(MemoryKeyValueStore):
    def __init__(self):
        super().__init__()
        self.get_all_calls = 0

    def get_all(self, namespace):
        self.get_all_calls += 1
        return super().get_all(namespace)


def test_tokens_today_does_not_scan_the_log():
    store = CountingStore()
    log = EgressLog(store, clock=lambda: NOW)
    key = log.record(tenant_id="acme", provider="p", model="m", payload_sha256="sha256:x", redacted_kinds={})
    log.update(key, input_tokens=5, output_tokens=7)
    assert log.tokens_today("acme") == 12 and store.get_all_calls == 0


def test_updating_a_row_twice_counts_the_difference_only():
    log = new_log()
    key = log.record(tenant_id="acme", provider="p", model="m", payload_sha256="sha256:x", redacted_kinds={})
    log.update(key, input_tokens=5, output_tokens=7)
    log.update(key, status="COMPLETED", input_tokens=5, output_tokens=9)
    assert log.tokens_today("acme") == 14


# F5 — budget enforced end to end over HTTP, including between ReAct turns of a stream ---------------------------


def _agent_with(gw):
    from hrms_plugin.agents.supervisor import SupervisorAgent

    agent = SupervisorAgent(llm_gateway=gw)
    agent.get_orchestrator()
    return agent


def test_http_chat_429_from_real_budget(make_client, bearer):
    log = new_log()
    key = log.record(tenant_id="acme", provider="fake", model="m", payload_sha256="sha256:x", redacted_kinds={})
    log.update(key, input_tokens=100)
    client = Client()
    res = make_client(supervisor_agent=_agent_with(gateway(client, budget=100, log=log))).post(
        "/v1/chat", headers=bearer(), json={"message": "hi", "jurisdiction": "IN"})
    assert res.status_code == 429 and res.json() == {"error": "LLM_BUDGET_EXHAUSTED"}
    assert client.calls == []


def test_http_stream_budget_exhausted_between_turns_is_reported_in_band(make_client, bearer):
    first = LLMResponse(content="", tool_calls=[ToolCall(id="c1", name="search_labor_regulations",
                                                         arguments={"query": "a"})], finish_reason="tool_use")
    client = Client(replies=[first])  # first turn uses 100 tokens = the whole budget
    agent = _agent_with(gateway(client, budget=100, log=new_log()))
    res = make_client(supervisor_agent=agent).post(
        "/v1/chat/stream", headers=bearer(), json={"message": "hi", "jurisdiction": "AE"})
    assert res.status_code == 200
    events = [json.loads(line[6:]) for line in res.text.splitlines() if line.startswith("data: ")]
    assert events[-1] == {"event": "error", "data": {"error": "LLM_BUDGET_EXHAUSTED"}}
    assert [c["kind"] for c in client.calls] == ["chat"]  # the synthesis call was never sent


# F2 — live evals need a registered tenant -----------------------------------------------------------------------


def test_live_eval_requires_a_tenant(monkeypatch):
    from hrms_plugin.evals import runner

    monkeypatch.setenv("HRMS_EVAL_LIVE_APPROVED", "1")
    monkeypatch.delenv("HRMS_EVAL_TENANT", raising=False)
    monkeypatch.setattr(runner, "LLMGateway", lambda: (_ for _ in ()).throw(AssertionError("no gateway")))
    assert runner.main(["--live"]) == 2


async def test_eval_cases_run_as_the_configured_tenant(scripted):
    from hrms_plugin.evals.runner import EvalCase, run_case
    from tests.support.scripted_llm import final

    gw, _ = scripted(final("hi"))
    tenants = []
    original = gw.preflight
    gw.preflight = lambda tenant, *tier: tenants.append(tenant) or original(tenant, *tier)
    await run_case(EvalCase("g", "hi", "IN", UserRole.EMPLOYEE, None), gw, tenant_id="acme")
    assert set(tenants) == {"acme"}


def test_live_eval_uses_the_tenant_from_env(monkeypatch):
    from hrms_plugin.evals import runner

    monkeypatch.setenv("HRMS_EVAL_LIVE_APPROVED", "1")
    monkeypatch.setenv("HRMS_EVAL_TENANT", "acme")
    seen = {}

    async def fake_run_all(gw, cases=None, tenant_id=None):
        seen["tenant"] = tenant_id
        return []

    monkeypatch.setattr(runner, "LLMGateway", lambda: object())
    monkeypatch.setattr(runner, "run_all", fake_run_all)
    runner.main(["--live"])
    assert seen["tenant"] == "acme"


# F8 — an oversized error keeps its error code -------------------------------------------------------------------


def test_oversized_error_output_keeps_the_error_code():
    out = json.loads(tool_result_content({"error": "SCHEMA_DRIFT", "details": "x" * 20000}))
    assert out["error"] == "TOOL_OUTPUT_TOO_LARGE" and out["tool_error"] == "SCHEMA_DRIFT"


@pytest.mark.parametrize("bad", [{"error": "x" * 101, "d": "y" * 20000}, {"error": {"nested": 1}, "d": "y" * 20000},
                                 ["y" * 20001]])
def test_oversized_output_without_a_short_string_error_code(bad):
    out = json.loads(tool_result_content(bad))
    assert out["error"] == "TOOL_OUTPUT_TOO_LARGE" and "tool_error" not in out
