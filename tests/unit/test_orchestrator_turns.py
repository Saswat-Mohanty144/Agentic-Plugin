"""Orchestrator turn structure, refusal and single-call streaming (piece E: E2, E3, E4)."""

from hrms_plugin.agents.orchestrator import AgenticOrchestrator
from hrms_plugin.agents.supervisor import UserIntent
from hrms_plugin.agents.tools import AgenticToolRegistry
from hrms_plugin.ai.llm import REFUSAL_TEXT, LLMResponse
from hrms_plugin.security.rbac import AuthContext, UserRole
from tests.support.scripted_llm import final, tool_call, tool_calls

ADMIN = AuthContext(user_id="t", tenant_id="acme", roles=frozenset({UserRole.SUPER_ADMIN}))


def orch(gateway):
    return AgenticOrchestrator(llm_gateway=gateway, tool_registry=AgenticToolRegistry())


async def test_parallel_tool_calls_become_one_assistant_message(scripted):
    gateway, client = scripted(
        tool_calls(("search_labor_regulations", {"query": "a"}), ("search_labor_regulations", {"query": "b"})),
        final("ok"),
    )
    await orch(gateway).run("q", jurisdiction="AE", auth=ADMIN)
    roles = [m.role for m in client.calls[1]["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "tool"]
    assistant = client.calls[1]["messages"][2]
    assert len(assistant.tool_calls) == 2
    assert [m.tool_call_id for m in client.calls[1]["messages"][3:]] == [tc.id for tc in assistant.tool_calls]


async def test_raw_provider_content_is_carried_into_the_next_turn(scripted):
    first = tool_call("search_labor_regulations", query="a")
    first.raw_content = ["RAW-BLOCK"]
    gateway, client = scripted(first, final("ok"))
    await orch(gateway).run("q", jurisdiction="AE", auth=ADMIN)
    assert client.calls[1]["messages"][2].raw_content == ["RAW-BLOCK"]


async def test_refusal_is_returned_and_no_tool_runs(scripted):
    gateway, _ = scripted(LLMResponse(content=REFUSAL_TEXT, finish_reason="refusal"))
    resp = await orch(gateway).run("q", jurisdiction="AE", auth=ADMIN)
    assert resp.reply_text == REFUSAL_TEXT
    assert resp.tool_calls == [] and resp.intent == UserIntent.GENERAL_HR


async def test_refusal_after_a_tool_ran(scripted):
    gateway, _ = scripted(tool_call("search_labor_regulations", query="a"),
                          LLMResponse(content=REFUSAL_TEXT, finish_reason="refusal"))
    resp = await orch(gateway).run("q", jurisdiction="AE", auth=ADMIN)
    assert resp.reply_text == REFUSAL_TEXT and resp.intent == UserIntent.LEGAL_QNA


async def test_stream_without_tools_calls_the_llm_once(scripted):
    gateway, client = scripted(final("hello world"))
    events = [e async for e in orch(gateway).run_stream("q", jurisdiction="AE", auth=ADMIN)]
    assert len(client.calls) == 1
    assert "".join(e["data"] for e in events if e["event"] == "token") == "hello world"
    assert events[-1] == {"event": "intent", "data": "GENERAL_HR"}


async def test_stream_with_empty_text_emits_no_tokens(scripted):
    gateway, _ = scripted(final(""))
    events = [e async for e in orch(gateway).run_stream("q", jurisdiction="AE", auth=ADMIN)]
    assert not [e for e in events if e["event"] == "token"]
    assert events[-1] == {"event": "intent", "data": "GENERAL_HR"}


async def test_stream_parallel_tools_one_assistant_message(scripted):
    gateway, client = scripted(
        tool_calls(("search_labor_regulations", {"query": "a"}), ("search_labor_regulations", {"query": "b"})),
        final("ok"),
    )
    [e async for e in orch(gateway).run_stream("q", jurisdiction="AE", auth=ADMIN)]
    assert [m.role for m in client.calls[1]["messages"]] == ["system", "user", "assistant", "tool", "tool"]
