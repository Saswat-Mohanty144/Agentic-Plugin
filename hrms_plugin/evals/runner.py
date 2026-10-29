"""Run the tool-routing eval set through the real orchestrator with tool execution stubbed out (piece H8).

Offline (CI): the caller supplies the LLM gateway (a scripted double) — proves the harness and scorer.
Live: `python -m hrms_plugin.evals.runner --live` uses the configured provider and SPENDS MONEY; it refuses to run
unless HRMS_EVAL_LIVE_APPROVED=1 is set by the person who approved the budget, and HRMS_EVAL_TENANT names a registered
tenant whose llm_policy allows the provider (the gateway fails closed for any other tenant).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import yaml

from hrms_plugin.agents.orchestrator import SUPER_AGENT_SYSTEM_PROMPT, AgenticOrchestrator
from hrms_plugin.agents.tools import AgenticToolRegistry
from hrms_plugin.ai.llm import LLMGateway
from hrms_plugin.security.rbac import AuthContext, UserRole

CASES_PATH = Path(__file__).with_name("cases.yaml")
EVAL_TENANT = "eval"
# Read-only tools that run for real in evals so their citations can ground the answer; the rest stay stubbed.
REAL_TOOLS = ("search_labor_regulations",)
_CITATION = re.compile(r"\b(?:Article|Section)\s+\d+[A-Za-z]?\b")
USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


@dataclass(frozen=True)
class EvalCase:
    id: str
    prompt: str
    jurisdiction: str
    role: UserRole
    expect_first_tool: Optional[str]
    forbid_tools: Sequence[str] = ()


@dataclass
class EvalResult:
    case_id: str
    passed: bool
    reason: str
    tools_called: List[str] = field(default_factory=list)
    tokens: Optional[int] = None  # provider-reported usage summed over the case; None when none was reported
    citations_ok: bool = True
    ungrounded_citations: List[str] = field(default_factory=list)


def load_cases(path: Path = CASES_PATH) -> List[EvalCase]:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict) or not isinstance(data.get("version"), int):
        raise ValueError("eval file needs an integer 'version'")
    cases = [
        EvalCase(
            id=c["id"],
            prompt=c["prompt"],
            jurisdiction=c["jurisdiction"],
            role=UserRole[c["role"]],
            expect_first_tool=c["expect_first_tool"],
            forbid_tools=tuple(c.get("forbid_tools") or ()),
        )
        for c in data["cases"]
    ]
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate eval case id")
    registry = AgenticToolRegistry()
    for c in cases:
        for name in [c.expect_first_tool, *c.forbid_tools]:
            if name is not None and registry.get_definition(name) is None:
                raise ValueError(f"case {c.id}: unknown tool {name!r}")
    return cases


def score(case: EvalCase, tools_called: Sequence[str]) -> EvalResult:
    called = list(tools_called)
    forbidden = [t for t in called if t in case.forbid_tools]
    if forbidden:
        return EvalResult(case.id, False, f"called forbidden tool(s) {forbidden}", called)
    first = called[0] if called else None
    if first != case.expect_first_tool:
        return EvalResult(case.id, False, f"first tool {first!r}, expected {case.expect_first_tool!r}", called)
    return EvalResult(case.id, True, "ok", called)


class _RecordingRegistry(AgenticToolRegistry):
    """Real definitions and permissions; execution is recorded and stubbed so evals never touch a host HRMS."""

    def __init__(self) -> None:
        super().__init__()
        self.called: List[str] = []
        self.outputs: List[Any] = []

    async def execute(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        self.called.append(tool_name)
        if tool_name in REAL_TOOLS:
            output = await super().execute(tool_name, arguments)
            self.outputs.append(output)
            return output
        return {"eval_stub": True, "tool": tool_name}


def _citations(text: str) -> List[str]:
    return sorted(set(_CITATION.findall(text or "")))


async def run_case(case: EvalCase, gateway: LLMGateway, tenant_id: str = EVAL_TENANT) -> EvalResult:
    registry = _RecordingRegistry()
    orchestrator = AgenticOrchestrator(llm_gateway=gateway, tool_registry=registry, max_iterations=2)
    auth = AuthContext(user_id="eval", tenant_id=tenant_id, roles=frozenset({case.role}))
    usages: List[Dict[str, Any]] = []
    original = gateway.client.chat

    async def counting(*args: Any, **kwargs: Any) -> Any:
        resp = await original(*args, **kwargs)
        if resp.usage:
            usages.append(resp.usage)
        return resp

    gateway.client.chat = counting  # type: ignore[method-assign]
    try:
        response = await orchestrator.run(case.prompt, jurisdiction=case.jurisdiction, auth=auth)
    finally:
        gateway.client.chat = original  # type: ignore[method-assign]
    names = [tc["name"] for tc in response.tool_calls]  # includes calls refused by RBAC
    result = score(case, names)
    result.tokens = sum(u.get(f) or 0 for u in usages for f in USAGE_FIELDS) if usages else None
    grounded = set(_citations(json.dumps(registry.outputs, default=str)))
    result.ungrounded_citations = [c for c in _citations(response.reply_text) if c not in grounded]
    if result.ungrounded_citations:
        result.citations_ok = False
        result.passed = False
        result.reason = f"{result.reason}; ungrounded citations {result.ungrounded_citations}"
    return result


def summarise(results: Sequence[EvalResult]) -> Dict[str, Any]:
    """Pass rate, median tokens per case (cases with reported usage) and citation grounding."""
    tokens = [r.tokens for r in results if r.tokens is not None]
    return {"cases": len(results), "passed": sum(r.passed for r in results),
            "median_tokens": statistics.median(tokens) if tokens else None,
            "cases_without_usage": len(results) - len(tokens),
            "ungrounded_citation_cases": sum(not r.citations_ok for r in results)}


def prefix_chars(role: UserRole) -> int:
    """Size of the cacheable prefix (tool definitions + frozen system prompt) a given role sends."""
    auth = AuthContext(user_id="eval", tenant_id=EVAL_TENANT, roles=frozenset({role}))
    tools = AgenticToolRegistry().definitions_for(auth)
    return len(json.dumps([t.to_anthropic_schema() for t in tools], sort_keys=True)) + len(SUPER_AGENT_SYSTEM_PROMPT)


async def run_all(
    gateway: LLMGateway, cases: Optional[List[EvalCase]] = None, tenant_id: str = EVAL_TENANT
) -> List[EvalResult]:
    return [await run_case(c, gateway, tenant_id) for c in (cases or load_cases())]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="call the configured LLM provider (costs money)")
    args = parser.parse_args(argv)
    if not args.live:
        print("offline evals run in the test suite: pytest tests/unit/test_evals.py")
        return 0
    if os.environ.get("HRMS_EVAL_LIVE_APPROVED") != "1":
        print("refusing: live evals spend money; set HRMS_EVAL_LIVE_APPROVED=1 once the budget is approved")
        return 2
    tenant_id = os.environ.get("HRMS_EVAL_TENANT", "")
    if not tenant_id:
        print("refusing: set HRMS_EVAL_TENANT to a registered tenant whose llm_policy allows the provider")
        return 2
    results = asyncio.run(run_all(LLMGateway(), tenant_id=tenant_id))
    for r in results:
        print(json.dumps(r.__dict__))
    passed = sum(r.passed for r in results)
    print(json.dumps(summarise(results)))
    print(f"{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
