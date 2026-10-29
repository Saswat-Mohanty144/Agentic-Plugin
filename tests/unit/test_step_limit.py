"""Token waste #5: reaching the step limit forces an answer from what was gathered, never a canned summary.

If the last allowed step still asks for tools, one more call is made with tool use forbidden (tools withheld, or kept
with tool_choice none where the provider supports it, so the cached prefix still matches). The reply is partial.
"""

import asyncio

from hrms_plugin.agents.orchestrator import AgenticOrchestrator
from hrms_plugin.agents.tools import AgenticToolRegistry
from hrms_plugin.ai.llm import LLMResponse, ToolCall
from hrms_plugin.security.rbac import AuthContext, UserRole
from tests.support.scripted_llm import final, scripted_gateway, tool_call

ADMIN = AuthContext(user_id="u", tenant_id="acme", roles=frozenset({UserRole.SUPER_ADMIN}))
SEARCH = lambda q: tool_call("search_labor_regulations", query=q)  # noqa: E731


def run(*responses, max_iterations=5, client_hook=None):
    gateway, client = scripted_gateway(responses)
    if client_hook:
        client_hook(client)
    orch = AgenticOrchestrator(llm_gateway=gateway, tool_registry=AgenticToolRegistry(), max_iterations=max_iterations)
    return asyncio.run(orch.run(user_message="labor code question", auth=ADMIN)), client


def test_limit_forces_a_final_answer_without_tools():
    resp, client = run(*[SEARCH(str(i)) for i in range(5)], final("Here is what I found."))
    assert resp.reply_text == "Here is what I found." and resp.partial is True
    assert len(client.calls) == 6 and client.calls[-1]["tools"] is None
    assert all(c["tools"] for c in client.calls[:5])
    assert "agentic reasoning steps" not in resp.reply_text


def test_forced_call_sees_every_tool_result():
    _, client = run(*[SEARCH(str(i)) for i in range(5)], final("ok"))
    assert sum(1 for m in client.calls[-1]["messages"] if m.role == "tool") == 5


def test_answer_within_the_limit_is_not_partial():
    resp, client = run(SEARCH("a"), final("done"))
    assert resp.reply_text == "done" and resp.partial is False and len(client.calls) == 2


def test_tool_calls_on_the_forced_call_are_ignored_and_reported_honestly():
    resp, client = run(*[SEARCH(str(i)) for i in range(5)], SEARCH("again"))
    assert resp.partial is True and len(client.calls) == 6
    assert "step limit" in resp.reply_text.lower() and len(resp.tool_calls) == 5  # the 6th call ran nothing


def test_provider_with_tool_choice_none_keeps_tools_but_forbids_them():
    seen = []

    def hook(client):
        client.supports_tool_choice_none = True
        original = client.chat

        async def chat(messages, tools=None, temperature=None, **kw):
            seen.append(kw.get("tool_choice"))
            return await original(messages, tools, temperature)

        client.chat = chat

    resp, client = run(SEARCH("a"), final("ok"), max_iterations=1, client_hook=hook)
    assert resp.partial is True and client.calls[-1]["tools"] and seen == [None, "none"]


def test_limit_of_one_step():
    resp, client = run(SEARCH("a"), LLMResponse(content="final", tool_calls=[], finish_reason="stop"),
                       max_iterations=1)
    assert resp.reply_text == "final" and resp.partial is True and len(client.calls) == 2


_ = ToolCall  # imported for type reference in scripted responses
