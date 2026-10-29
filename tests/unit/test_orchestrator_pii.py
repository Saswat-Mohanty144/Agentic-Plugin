"""Orchestrator never shows context values or identifiers to the LLM; tools still get real values (piece C §3.3)."""

import re

from hrms_plugin.agents.orchestrator import AgenticOrchestrator
from hrms_plugin.agents.tools import AgenticToolRegistry
from hrms_plugin.ai.llm import BaseLLMClient, LLMConfig, LLMGateway, LLMProvider, LLMResponse, ToolCall
from hrms_plugin.security.rbac import AuthContext, UserRole
from tests.support.scripted_llm import final, tool_call

ADMIN = AuthContext(user_id="t", tenant_id="acme", roles=frozenset({UserRole.SUPER_ADMIN}))
EMPLOYEES = [{"id": "EMP-901", "name": "Tariq Al-Mansoor", "email": "tariq@acme.test", "basic_salary": 4000.0,
              "gross_salary": 10000.0, "probation_days": 210, "weekly_hours": 54, "uae_wps_registered": False}]
TOKEN_RE = re.compile(r"\[PII_[A-Z_]+_\d+_[0-9a-f]{8}\]")


def everything_sent(client):
    parts = []
    for call in client.calls:
        for m in call["messages"]:
            parts.append(m.content or "")
            parts += [str(tc.arguments) for tc in (m.tool_calls or [])]
    return "\n".join(parts)


def orch(gateway):
    return AgenticOrchestrator(llm_gateway=gateway, tool_registry=AgenticToolRegistry())


async def test_system_prompt_lists_context_keys_not_values(scripted):
    gateway, client = scripted(final("ok"))
    await orch(gateway).run("go", jurisdiction="AE", auth=ADMIN,
                            context={"employees": EMPLOYEES, "annual_ctc": 1800000, "record_id": "EMP-901"})
    system = client.calls[0]["messages"][0].content
    assert "annual_ctc" in system and "employees" in system
    assert "1800000" not in system and "Tariq" not in system and "4000" not in system
    assert "record_id=EMP-901" in system  # allowlisted navigation key


async def test_context_names_never_reach_any_llm_call(scripted):
    gateway, client = scripted(tool_call("audit_statutory_compliance"), final("done"))
    resp = await orch(gateway).run("audit", jurisdiction="AE", auth=ADMIN, context={"employees": EMPLOYEES})
    sent = everything_sent(client)
    assert "Tariq" not in sent and "tariq@acme.test" not in sent
    assert resp.tool_calls[0]["arguments"]["employees"] == EMPLOYEES  # tool still got the real records


async def test_context_value_is_injected_into_matching_tool_parameter(scripted):
    gateway, _ = scripted(tool_call("calculate_salary_structure"), final("ok"))
    resp = await orch(gateway).run("structure it", jurisdiction="IN", auth=ADMIN, context={"gross_or_ctc": 1800000})
    assert resp.tool_calls[0]["arguments"]["gross_or_ctc"] == 1800000
    assert resp.structured_data["annual_ctc"] == 1800000


async def test_request_jurisdiction_beats_context_jurisdiction(scripted):
    gateway, _ = scripted(tool_call("search_labor_regulations", query="gratuity"), final("ok"))
    resp = await orch(gateway).run("q", jurisdiction="AE", auth=ADMIN, context={"jurisdiction": "IN", "tenant_id": "x"})
    args = resp.tool_calls[0]["arguments"]
    assert args["jurisdiction"] == "AE"


async def test_identifier_from_tool_output_is_absent_from_next_llm_call(scripted):
    gateway, client = scripted(
        tool_call(
            "evaluate_onboarding_offer", candidate_name="Deepak Patel", job_title="Engineer", annual_salary=1200000
        ),
        final("ok"),
    )
    await orch(gateway).run("evaluate", jurisdiction="IN", auth=ADMIN)
    second_call = "\n".join((m.content or "") + str([tc.arguments for tc in (m.tool_calls or [])])
                            for m in client.calls[1]["messages"])
    assert "Deepak" not in second_call


class TokenEchoingClient(BaseLLMClient):
    """First turn: asks the tool to act on whatever token it received for the candidate; then finishes."""

    def __init__(self):
        super().__init__(LLMConfig(provider=LLMProvider.FAKE))
        self.calls = []

    async def chat(self, messages, tools=None, temperature=None):
        self.calls.append({"messages": messages, "tools": tools})
        if len(self.calls) == 1:
            token = TOKEN_RE.search(messages[-1].content).group(0)
            args = {"candidate_name": token, "job_title": "Engineer", "annual_salary": 1200000}
            call = ToolCall(id="c1", name="evaluate_onboarding_offer", arguments=args)
            return LLMResponse(content="", tool_calls=[call], finish_reason="tool_calls")
        return LLMResponse(content="done")

    async def chat_stream(self, messages, tools=None, temperature=None):
        raise AssertionError("not used")
        yield {}


async def test_tokens_written_by_the_llm_reach_tools_as_real_values():
    client = TokenEchoingClient()
    gateway = LLMGateway(config=LLMConfig(provider=LLMProvider.FAKE))
    gateway.client = client
    resp = await orch(gateway).run("Evaluate the offer for Deepak Patel", jurisdiction="IN", auth=ADMIN,
                                   context={"candidate_name": "Deepak Patel"})
    assert "Deepak" not in client.calls[0]["messages"][-1].content
    assert resp.tool_calls[0]["arguments"]["candidate_name"] == "Deepak Patel"


async def test_stream_path_also_hides_context_values(scripted):
    gateway, client = scripted(tool_call("audit_statutory_compliance"), final("done"))
    events = [e async for e in orch(gateway).run_stream("audit", jurisdiction="AE", auth=ADMIN,
                                                        context={"employees": EMPLOYEES})]
    assert "Tariq" not in everything_sent(client)
    assert events[-1] == {"event": "intent", "data": "COMPLIANCE_AUDIT"}


async def test_tool_output_with_decimals_does_not_crash_the_run(scripted):
    """Pre-existing (baseline 3028359): json.dumps(tool_output) raised on Decimal values."""
    gateway, _ = scripted(
        tool_call("evaluate_onboarding_offer", candidate_name="X Y", job_title="Engineer", annual_salary=1200000),
        final("ok"),
    )
    resp = await orch(gateway).run("evaluate", jurisdiction="IN", auth=ADMIN)
    assert resp.reply_text == "ok"


async def test_remediation_actions_identify_the_record(scripted):
    """Piece D: the SDK needs the violation's entity to target the change (no hard-coded EMP-AUTO)."""
    gateway, _ = scripted(tool_call("audit_statutory_compliance"), final("done"))
    resp = await orch(gateway).run("audit", jurisdiction="AE", auth=ADMIN, context={"employees": EMPLOYEES})
    assert resp.suggested_actions
    remediations = [a for a in resp.suggested_actions if a.get("action") == "REMEDIATE_PATCH"]
    assert remediations
    assert all(a["entity_id"] == "EMP-901" and a["entity_type"] == "EMPLOYEE" for a in remediations)
