"""Tests for Gap B / Decision D2: Autonomous Agentic Streaming ReAct Loop.

Verifies:
- 0, 1, 2 tool rounds streaming progression
- boundary: exactly max_iterations tool rounds
- boundary: max_iterations+1 (partial event emitted before tokens)
- truncated true/false on finish_reason (LENGTH_STOP_REASONS)
- grounding refusal (UNGROUNDED_AMOUNT) inside the stream loop
- PII restoration in streamed tokens
"""

import pytest

from hrms_plugin.agents.orchestrator import AgenticOrchestrator
from hrms_plugin.agents.supervisor import UserIntent
from hrms_plugin.agents.tools import AgenticToolRegistry
from hrms_plugin.ai.llm import LLMResponse, ToolCall
from hrms_plugin.security.rbac import AuthContext, UserRole
from tests.support.scripted_llm import final, tool_call

ADMIN = AuthContext(user_id="u-admin", tenant_id="acme", roles=frozenset({UserRole.SUPER_ADMIN}))


def _orch(gateway, max_iterations=5):
    return AgenticOrchestrator(
        llm_gateway=gateway,
        tool_registry=AgenticToolRegistry(),
        max_iterations=max_iterations,
    )


async def _collect(gen):
    return [e async for e in gen]


@pytest.mark.asyncio
async def test_stream_0_tool_rounds_positive(scripted):
    """Positive: 0 tool rounds formulation streams tokens directly without extra LLM call."""
    gateway, client = scripted(final("Direct answer without tools."))
    orch = _orch(gateway)
    events = await _collect(orch.run_stream("What is the probation period?", jurisdiction="AE", auth=ADMIN))

    assert len(client.calls) == 1
    event_names = [e["event"] for e in events]
    assert "tool_call" not in event_names
    assert "tool_observation" not in event_names
    assert "partial" not in event_names

    tokens = [e["data"] for e in events if e["event"] == "token"]
    assert "".join(tokens) == "Direct answer without tools."

    done_ev = next(e for e in events if e["event"] == "done")
    assert done_ev["data"] == {"finish_reason": "stop", "truncated": False}

    assert events[-1] == {"event": "intent", "data": UserIntent.GENERAL_HR.value}


@pytest.mark.asyncio
async def test_stream_1_tool_round_positive(scripted):
    """Positive: 1 tool round emits tool_call/observation, calls LLM, and streams tokens."""
    gateway, client = scripted(
        tool_call("search_labor_regulations", query="probation period"),
        final("Probation period is up to 6 months."),
    )
    orch = _orch(gateway)
    events = await _collect(orch.run_stream("Check probation law", jurisdiction="AE", auth=ADMIN))

    assert len(client.calls) == 2
    event_names = [e["event"] for e in events]
    assert event_names.count("tool_call") == 1
    assert event_names.count("tool_observation") == 1
    assert "partial" not in event_names

    tc_ev = next(e for e in events if e["event"] == "tool_call")
    assert tc_ev["data"]["name"] == "search_labor_regulations"

    tokens = [e["data"] for e in events if e["event"] == "token"]
    assert "".join(tokens) == "Probation period is up to 6 months."

    done_ev = next(e for e in events if e["event"] == "done")
    assert done_ev["data"] == {"finish_reason": "stop", "truncated": False}

    assert events[-1] == {"event": "intent", "data": UserIntent.LEGAL_QNA.value}


@pytest.mark.asyncio
async def test_stream_2_tool_rounds_positive(scripted):
    """Positive: 2 tool rounds sequentially execute before streaming final answer."""
    gateway, client = scripted(
        tool_call("search_labor_regulations", query="gratuity rules"),
        tool_call("calculate_eosb_gratuity", basic_wage=5000, tenure_years=3),
        final("Gratuity calculated under UAE labor law."),
    )
    orch = _orch(gateway)
    events = await _collect(
        orch.run_stream(
            "What is gratuity for basic 5000 and 3 years?",
            jurisdiction="AE",
            context={"basic_wage": 5000, "tenure_years": 3},
            auth=ADMIN,
        )
    )

    assert len(client.calls) == 3
    event_names = [e["event"] for e in events]
    assert event_names.count("tool_call") == 2
    assert event_names.count("tool_observation") == 2
    assert "partial" not in event_names

    tool_calls_emitted = [e["data"]["name"] for e in events if e["event"] == "tool_call"]
    assert tool_calls_emitted == ["search_labor_regulations", "calculate_eosb_gratuity"]

    tokens = [e["data"] for e in events if e["event"] == "token"]
    assert "".join(tokens) == "Gratuity calculated under UAE labor law."

    done_ev = next(e for e in events if e["event"] == "done")
    assert done_ev["data"] == {"finish_reason": "stop", "truncated": False}


@pytest.mark.asyncio
async def test_stream_boundary_exactly_max_iterations(scripted):
    """Boundary: Finishing on exactly max_iterations without tool request is not partial."""
    gateway, client = scripted(
        tool_call("search_labor_regulations", query="step 1"),
        final("Resolved on second iteration within max_iterations=2."),
    )
    orch = _orch(gateway, max_iterations=2)
    events = await _collect(orch.run_stream("Search and answer", jurisdiction="AE", auth=ADMIN))

    assert len(client.calls) == 2
    event_names = [e["event"] for e in events]
    assert event_names.count("tool_call") == 1
    assert "partial" not in event_names

    tokens = [e["data"] for e in events if e["event"] == "token"]
    assert "".join(tokens) == "Resolved on second iteration within max_iterations=2."

    done_ev = next(e for e in events if e["event"] == "done")
    assert done_ev["data"] == {"finish_reason": "stop", "truncated": False}


@pytest.mark.asyncio
async def test_stream_boundary_max_iterations_plus_1_partial(scripted):
    """Boundary: Hitting max_iterations while still requesting tools emits partial and forces text-only stream."""
    gateway, client = scripted(
        tool_call("search_labor_regulations", query="step 1"),
        tool_call("search_labor_regulations", query="step 2"),
        final("Step limit reached; answering from gathered evidence."),
    )
    orch = _orch(gateway, max_iterations=2)
    events = await _collect(orch.run_stream("Exhaustive multi-step search", jurisdiction="AE", auth=ADMIN))

    assert len(client.calls) == 3
    event_names = [e["event"] for e in events]
    assert event_names.count("tool_call") == 2
    assert "partial" in event_names

    # Partial event must arrive before tokens
    partial_idx = event_names.index("partial")
    first_token_idx = event_names.index("token")
    assert partial_idx < first_token_idx
    assert events[partial_idx] == {"event": "partial", "data": True}

    tokens = [e["data"] for e in events if e["event"] == "token"]
    assert "".join(tokens).strip() == "Step limit reached; answering from gathered evidence."

    done_ev = next(e for e in events if e["event"] == "done")
    assert done_ev["data"]["finish_reason"] == "stop"
    assert done_ev["data"]["truncated"] is False

    assert events[-1]["event"] == "intent"


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["length", "max_tokens", "MAX_TOKENS"])
async def test_stream_truncated_true_on_length_stop_reasons(scripted, reason):
    """Positive/Boundary: done event reports truncated=True when finish_reason in LENGTH_STOP_REASONS."""
    gateway, _ = scripted(
        LLMResponse(content="Cut off reply...", finish_reason=reason, model="scripted", provider="scripted")
    )
    orch = _orch(gateway)
    events = await _collect(orch.run_stream("Long query", jurisdiction="AE", auth=ADMIN))

    done_ev = next(e for e in events if e["event"] == "done")
    assert done_ev["data"] == {"finish_reason": reason, "truncated": True}


@pytest.mark.asyncio
async def test_stream_truncated_false_on_normal_stop(scripted):
    """Positive: done event reports truncated=False on standard stop finish_reason."""
    gateway, _ = scripted(final("Normal complete reply."))
    orch = _orch(gateway)
    events = await _collect(orch.run_stream("Normal query", jurisdiction="AE", auth=ADMIN))

    done_ev = next(e for e in events if e["event"] == "done")
    assert done_ev["data"] == {"finish_reason": "stop", "truncated": False}


@pytest.mark.asyncio
async def test_stream_grounding_refusal_inside_stream_loop(scripted):
    """Negative/Boundary: Ungrounded tool call is refused in-loop with UNGROUNDED_AMOUNT observation."""
    bad_call = ToolCall(
        id="c-ungrounded",
        name="calculate_eosb_gratuity",
        arguments={"basic_wage": 99999, "tenure_years": 10},
    )
    gateway, client = scripted(
        LLMResponse(content="", tool_calls=[bad_call], finish_reason="tool_calls", model="s", provider="s"),
        final("Please specify your basic wage and tenure."),
    )
    orch = _orch(gateway)
    events = await _collect(orch.run_stream("Calculate my gratuity", jurisdiction="AE", auth=ADMIN))

    obs_ev = next(e for e in events if e["event"] == "tool_observation")
    assert obs_ev["data"]["output"] == {
        "success": False,
        "error": "UNGROUNDED_AMOUNT",
        "params": ["basic_wage", "tenure_years"],
    }

    # Verify tool result error was fed back to second LLM turn
    second_call_messages = client.calls[1]["messages"]
    tool_msg = next(m for m in second_call_messages if m.role == "tool")
    assert "UNGROUNDED_AMOUNT" in tool_msg.content

    tokens = [e["data"] for e in events if e["event"] == "token"]
    assert "".join(tokens) == "Please specify your basic wage and tenure."


@pytest.mark.asyncio
async def test_stream_pii_restoration_in_streamed_tokens(scripted):
    """Security: PII pseudonyms are restored to original user text in streamed token events."""
    gateway, client = scripted(
        tool_call("search_labor_regulations", query="regulations"),
        final("Verified employee record for Tariq Mansoor."),
    )
    orch = _orch(gateway)
    events = await _collect(
        orch.run_stream(
            "Verify Tariq Mansoor record",
            jurisdiction="AE",
            context={"employee_name": "Tariq Mansoor"},
            auth=ADMIN,
        )
    )

    # First turn LLM call should have redacted Tariq Mansoor
    first_call_text = client.calls[0]["messages"][-1].content
    assert "Tariq Mansoor" not in first_call_text

    tokens = [e["data"] for e in events if e["event"] == "token"]
    full_streamed = "".join(tokens)
    assert "Tariq Mansoor" in full_streamed
    assert "_PII_" not in full_streamed


# Follow-up review finding: stream result event with citations, actions, partial and truncated


@pytest.mark.asyncio
async def test_stream_result_event_no_tool_path(scripted):
    """Positive: No-tool path emits empty citations/actions in result event immediately before intent."""
    gateway, _ = scripted(final("Direct answer."))
    orch = _orch(gateway)
    events = await _collect(orch.run_stream("What is the company policy?", jurisdiction="AE", auth=ADMIN))

    assert events[-3]["event"] == "done"
    assert events[-2]["event"] == "result"
    assert events[-1]["event"] == "intent"

    result_data = events[-2]["data"]
    assert result_data == {
        "statutory_citations": [],
        "suggested_actions": [],
        "partial": False,
        "truncated": False,
    }


@pytest.mark.asyncio
async def test_stream_result_event_no_tool_path_truncated(scripted):
    """Boundary: No-tool path with length stop reason marks truncated=True in result event."""
    gateway, _ = scripted(
        LLMResponse(content="Cut off text...", finish_reason="length", model="scripted", provider="scripted")
    )
    orch = _orch(gateway)
    events = await _collect(orch.run_stream("Tell me everything", jurisdiction="AE", auth=ADMIN))

    assert events[-3]["event"] == "done"
    assert events[-2]["event"] == "result"
    assert events[-1]["event"] == "intent"

    result_data = events[-2]["data"]
    assert result_data == {
        "statutory_citations": [],
        "suggested_actions": [],
        "partial": False,
        "truncated": True,
    }


@pytest.mark.asyncio
async def test_stream_result_event_tool_path(scripted):
    """Positive: Tool path populates citations and actions in result event and deduplicates citations."""
    gateway, _ = scripted(
        tool_call(
            "audit_statutory_compliance",
            employees=[{"employee_id": "EMP-01", "name": "Alice", "probation_days": 200}],
        ),
        tool_call("search_labor_regulations", query="probation period limit"),
        final("Probation is capped at 180 days under Article 9."),
    )
    orch = _orch(gateway)
    events = await _collect(orch.run_stream("Audit probation of 200 days for Alice", jurisdiction="AE", auth=ADMIN))

    assert events[-3]["event"] == "done"
    assert events[-2]["event"] == "result"
    assert events[-1]["event"] == "intent"

    result_data = events[-2]["data"]
    assert result_data["partial"] is False
    assert result_data["truncated"] is False

    # Citations from tools deduplicated
    citations = result_data["statutory_citations"]
    assert len(citations) >= 1
    assert len(citations) == len(set(citations))
    assert any("Article 9" in c or "Decree-Law" in c for c in citations)

    # Actions from tool output
    actions = result_data["suggested_actions"]
    assert len(actions) == 1
    assert actions[0]["action"] == "REMEDIATE_PATCH"
    assert actions[0]["patch"] == {"probation_days": 180}


@pytest.mark.asyncio
async def test_stream_result_event_tool_path_truncated(scripted):
    """Boundary: Tool path where final response finishes with length reason marks truncated=True."""
    gateway, _ = scripted(
        tool_call(
            "audit_statutory_compliance",
            employees=[{"employee_id": "EMP-01", "name": "Alice", "probation_days": 200}],
        ),
        LLMResponse(content="Audited...", finish_reason="max_tokens", model="scripted", provider="scripted"),
    )
    orch = _orch(gateway)
    events = await _collect(orch.run_stream("Audit probation of 200 days for Alice", jurisdiction="AE", auth=ADMIN))

    assert events[-3]["event"] == "done"
    assert events[-2]["event"] == "result"
    assert events[-1]["event"] == "intent"

    result_data = events[-2]["data"]
    assert result_data["partial"] is False
    assert result_data["truncated"] is True
    assert len(result_data["statutory_citations"]) >= 1
    assert len(result_data["suggested_actions"]) == 1


@pytest.mark.asyncio
async def test_stream_result_event_partial_path(scripted):
    """Boundary: Partial path (hitting max_iterations) marks partial=True in result event."""
    gateway, _ = scripted(
        tool_call(
            "audit_statutory_compliance",
            employees=[{"employee_id": "EMP-01", "name": "Alice", "probation_days": 200}],
        ),
        final("Fallback answer after reaching step cap."),
    )
    orch = _orch(gateway, max_iterations=1)
    events = await _collect(orch.run_stream("Audit probation of 200 days for Alice", jurisdiction="AE", auth=ADMIN))

    # Partial event must be emitted before tokens
    event_names = [e["event"] for e in events]
    assert "partial" in event_names

    assert events[-3]["event"] == "done"
    assert events[-2]["event"] == "result"
    assert events[-1]["event"] == "intent"

    result_data = events[-2]["data"]
    assert result_data["partial"] is True
    assert result_data["truncated"] is False
    assert len(result_data["statutory_citations"]) >= 1
    assert len(result_data["suggested_actions"]) == 1


@pytest.mark.asyncio
async def test_stream_result_event_partial_path_truncated(scripted):
    """Boundary: Partial path where fallback stream finishes with length reason marks partial and truncated."""
    gateway, _ = scripted(
        tool_call(
            "audit_statutory_compliance",
            employees=[{"employee_id": "EMP-01", "name": "Alice", "probation_days": 200}],
        ),
        LLMResponse(content="Fallback truncated...", finish_reason="length", model="scripted", provider="scripted"),
    )
    orch = _orch(gateway, max_iterations=1)
    events = await _collect(orch.run_stream("Audit probation of 200 days for Alice", jurisdiction="AE", auth=ADMIN))

    assert events[-3]["event"] == "done"
    assert events[-2]["event"] == "result"
    assert events[-1]["event"] == "intent"

    result_data = events[-2]["data"]
    assert result_data["partial"] is True
    assert result_data["truncated"] is True
    assert len(result_data["statutory_citations"]) >= 1
    assert len(result_data["suggested_actions"]) == 1

