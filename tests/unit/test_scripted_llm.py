"""Tests for the deterministic LLM double used by orchestrator and route tests."""

import pytest

from hrms_plugin.ai.llm import ChatMessage
from tests.support.scripted_llm import ScriptedLLMClient, assert_consumed, final, tool_call

MSG = [ChatMessage(role="user", content="hi")]


async def test_returns_scripted_responses_in_order():
    client = ScriptedLLMClient([tool_call("search_labor_regulations", query="gratuity"), final("done")])
    first = await client.chat(MSG)
    second = await client.chat(MSG)
    assert first.tool_calls[0].name == "search_labor_regulations"
    assert first.tool_calls[0].arguments == {"query": "gratuity"}
    assert second.content == "done"
    assert client.remaining == 0
    assert len(client.calls) == 2


async def test_records_tools_passed_on_each_call():
    client = ScriptedLLMClient([final("ok")])
    await client.chat(MSG, tools=None)
    assert client.calls[0]["tools"] is None


async def test_extra_call_raises():
    client = ScriptedLLMClient([final("only")])
    await client.chat(MSG)
    with pytest.raises(AssertionError, match="unscripted call #2"):
        await client.chat(MSG)


async def test_empty_script_fails_on_first_call():
    with pytest.raises(AssertionError, match="unscripted call #1"):
        await ScriptedLLMClient([]).chat(MSG)


def test_leftover_responses_fail_consumption_check():
    with pytest.raises(AssertionError, match="1 scripted response"):
        assert_consumed([ScriptedLLMClient([final("unused")])])


def test_consumption_check_passes_when_all_used():
    assert_consumed([ScriptedLLMClient([])])


async def test_chat_stream_emits_tokens_then_done():
    client = ScriptedLLMClient([final("a b")])
    events = [e async for e in client.chat_stream(MSG)]
    assert events == [
        {"event": "token", "data": "a "},
        {"event": "token", "data": "b "},
        {"event": "done", "data": {"finish_reason": "stop"}},
    ]
