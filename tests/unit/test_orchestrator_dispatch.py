"""Orchestrator dispatch: intent from executed tools, trusted argument injection, unknown tools (spec A4/A5)."""

import inspect

import pytest

from hrms_plugin.agents.orchestrator import AgenticOrchestrator, build_trusted_arguments
from hrms_plugin.agents.supervisor import UserIntent
from hrms_plugin.agents.tools import AgenticToolRegistry
from hrms_plugin.ai.llm import ToolDefinition
from tests.support.scripted_llm import final, tool_call, tool_calls
from tests.unit.test_tool_intents import EXPECTED

EMPLOYEE = {"id": "EMP-901", "basic_salary": 4000.0, "gross_salary": 10000.0, "probation_days": 210,
            "weekly_hours": 54, "uae_wps_registered": False}


def _orchestrator(gateway, max_iterations=5):
    return AgenticOrchestrator(llm_gateway=gateway, tool_registry=AgenticToolRegistry(), max_iterations=max_iterations)


async def _run(orch, **kw):
    kw.setdefault("jurisdiction", "AE")
    kw.setdefault("tenant_id", "acme")
    return await orch.run(user_message="go", **kw)


async def _stream(orch, **kw):
    kw.setdefault("jurisdiction", "AE")
    kw.setdefault("tenant_id", "acme")
    return [e async for e in orch.run_stream(user_message="go", **kw)]


# A4 — intent ----------------------------------------------------------------

@pytest.mark.parametrize("tool", sorted(EXPECTED))
async def test_intent_is_the_executed_tools_intent(scripted, tool):
    gateway, _ = scripted(tool_call(tool), final("ok"))
    resp = await _run(_orchestrator(gateway))
    assert resp.intent == EXPECTED[tool]


async def test_no_tool_calls_gives_general_hr(scripted):
    gateway, _ = scripted(final("hello"))
    assert (await _run(_orchestrator(gateway))).intent == UserIntent.GENERAL_HR


async def test_first_executed_tool_wins(scripted):
    gateway, _ = scripted(
        tool_calls(("calculate_eosb_gratuity", {}), ("search_labor_regulations", {"query": "q"})), final("ok"))
    assert (await _run(_orchestrator(gateway))).intent == UserIntent.PAYROLL_EOSB


async def test_step_limit_still_reports_intent(scripted):
    gateway, _ = scripted(tool_call("search_labor_regulations", query="a"),
                          tool_call("search_labor_regulations", query="b"))
    resp = await _run(_orchestrator(gateway, max_iterations=2))
    assert resp.intent == UserIntent.LEGAL_QNA
    assert resp.reply_text.startswith("Completed")


async def test_unknown_tool_is_not_executed(scripted):
    gateway, _ = scripted(tool_call("drop_database"), final("ok"))
    resp = await _run(_orchestrator(gateway))
    assert resp.intent == UserIntent.GENERAL_HR
    assert resp.tool_calls[0]["name"] == "drop_database"
    assert resp.structured_data is None


async def test_unknown_tool_observation_is_reported_to_llm(scripted):
    gateway, client = scripted(tool_call("drop_database"), final("ok"))
    await _run(_orchestrator(gateway))
    tool_message = client.calls[1]["messages"][-1]
    assert tool_message.role == "tool" and "UNKNOWN_TOOL" in tool_message.content


# A5 — trusted arguments ---------------------------------------------------

async def test_missing_jurisdiction_is_injected(scripted):
    gateway, _ = scripted(tool_call("search_labor_regulations", query="gratuity"), final("ok"))
    resp = await _run(_orchestrator(gateway), jurisdiction="AE")
    assert resp.tool_calls[0]["arguments"]["jurisdiction"] == "AE"
    assert resp.tool_calls[0]["overrides"] == []


async def test_llm_jurisdiction_is_overridden_and_recorded(scripted):
    gateway, _ = scripted(tool_call("search_labor_regulations", query="gratuity", jurisdiction="IN"), final("ok"))
    resp = await _run(_orchestrator(gateway), jurisdiction="AE")
    call = resp.tool_calls[0]
    assert call["arguments"]["jurisdiction"] == "AE"
    assert call["overrides"] == [{"param": "jurisdiction", "llm_value": "IN", "trusted_value": "AE"}]


async def test_tenant_id_is_injected_from_request(scripted):
    gateway, _ = scripted(tool_call("audit_statutory_compliance", tenant_id="other"), final("ok"))
    resp = await _run(_orchestrator(gateway), tenant_id="acme", context={"employees": [EMPLOYEE]})
    assert resp.tool_calls[0]["arguments"]["tenant_id"] == "acme"


async def test_context_employees_are_injected(scripted):
    gateway, _ = scripted(tool_call("audit_statutory_compliance"), final("ok"))
    resp = await _run(_orchestrator(gateway), context={"employees": [EMPLOYEE]})
    assert resp.tool_calls[0]["arguments"]["employees"] == [EMPLOYEE]
    assert resp.structured_data["violations"]  # AE rules on this record produce violations


async def test_llm_employees_are_replaced_by_context(scripted):
    gateway, _ = scripted(tool_call("audit_statutory_compliance", employees=[{"id": "FAKE"}]), final("ok"))
    resp = await _run(_orchestrator(gateway), context={"employees": [EMPLOYEE]})
    call = resp.tool_calls[0]
    assert call["arguments"]["employees"] == [EMPLOYEE]
    assert {"param": "employees", "llm_value": [{"id": "FAKE"}], "trusted_value": [EMPLOYEE]} in call["overrides"]


async def test_context_without_employees_injects_nothing(scripted):
    gateway, _ = scripted(tool_call("audit_statutory_compliance"), final("ok"))
    resp = await _run(_orchestrator(gateway), context={})
    assert "employees" not in resp.tool_calls[0]["arguments"]


def test_tool_without_jurisdiction_param_gets_no_extra_argument():
    definition = AgenticToolRegistry().get_definition("audit_job_description_bias")
    args, overrides = build_trusted_arguments(definition, {"jd_text": "x"}, {"jurisdiction": "AE", "tenant_id": "t"})
    assert args == {"jd_text": "x"} and overrides == []


def test_build_trusted_arguments_ignores_undeclared_params():
    definition = ToolDefinition(name="t", description="", parameters={"properties": {"a": {}}})
    assert build_trusted_arguments(definition, {}, {"b": 1}) == ({}, [])


@pytest.mark.parametrize("handler", [
    "_tool_calculate_salary", "_tool_calculate_eosb", "_tool_audit_compliance", "_tool_search_laws",
    "_tool_evaluate_offer", "_tool_verify_docs", "_tool_compute_final_settlement",
])
def test_jurisdiction_is_required_on_handlers(handler):
    param = inspect.signature(getattr(AgenticToolRegistry(), handler)).parameters["jurisdiction"]
    assert param.default is inspect.Parameter.empty


def test_direct_call_without_jurisdiction_raises():
    with pytest.raises(TypeError):
        AgenticToolRegistry()._tool_search_laws(query="gratuity")


# Parity between run() and run_stream() ----------------------------------------

async def test_stream_reports_intent_and_injects_same_arguments(scripted):
    gateway, _ = scripted(tool_call("search_labor_regulations", query="q", jurisdiction="IN"), final("ok"))
    events = await _stream(_orchestrator(gateway), jurisdiction="AE")
    observation = next(e for e in events if e["event"] == "tool_observation")
    assert observation["data"]["arguments"]["jurisdiction"] == "AE"
    assert observation["data"]["overrides"] == [{"param": "jurisdiction", "llm_value": "IN", "trusted_value": "AE"}]
    assert events[-1] == {"event": "intent", "data": "LEGAL_QNA"}


async def test_stream_without_tools_reports_general_hr(scripted):
    # run_stream calls the LLM twice when no tool is used (known waste, fixed in piece E).
    gateway, _ = scripted(final("hi"), final("hi"))
    events = await _stream(_orchestrator(gateway))
    assert events[-1] == {"event": "intent", "data": "GENERAL_HR"}


async def test_stream_unknown_tool_not_executed(scripted):
    gateway, _ = scripted(tool_call("drop_database"), final("ok"))
    events = await _stream(_orchestrator(gateway))
    observation = next(e for e in events if e["event"] == "tool_observation")
    assert observation["data"]["output"] == {"error": "UNKNOWN_TOOL", "tool": "drop_database"}
    assert events[-1] == {"event": "intent", "data": "GENERAL_HR"}
