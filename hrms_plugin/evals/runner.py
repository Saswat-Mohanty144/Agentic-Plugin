"""Run the tool-routing eval set through the real orchestrator with tool execution stubbed out (piece H8).

Offline (CI): the caller supplies the LLM gateway (a scripted double) — proves the harness and scorer.
Live: `python -m hrms_plugin.evals.runner --live` uses the configured provider and SPENDS MONEY; it refuses to run
unless HRMS_EVAL_LIVE_APPROVED=1 is set by the person who approved the budget.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
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

    async def execute(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        self.called.append(tool_name)
        return {"eval_stub": True, "tool": tool_name}


async def run_case(case: EvalCase, gateway: LLMGateway) -> EvalResult:
    registry = _RecordingRegistry()
    orchestrator = AgenticOrchestrator(llm_gateway=gateway, tool_registry=registry, max_iterations=2)
    auth = AuthContext(user_id="eval", tenant_id=EVAL_TENANT, roles=frozenset({case.role}))
    response = await orchestrator.run(case.prompt, jurisdiction=case.jurisdiction, auth=auth)
    names = [tc["name"] for tc in response.tool_calls]  # includes calls refused by RBAC
    return score(case, names)


def prefix_chars(role: UserRole) -> int:
    """Size of the cacheable prefix (tool definitions + frozen system prompt) a given role sends."""
    auth = AuthContext(user_id="eval", tenant_id=EVAL_TENANT, roles=frozenset({role}))
    tools = AgenticToolRegistry().definitions_for(auth)
    return len(json.dumps([t.to_anthropic_schema() for t in tools], sort_keys=True)) + len(SUPER_AGENT_SYSTEM_PROMPT)


async def run_all(gateway: LLMGateway, cases: Optional[List[EvalCase]] = None) -> List[EvalResult]:
    return [await run_case(c, gateway) for c in (cases or load_cases())]


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
    results = asyncio.run(run_all(LLMGateway()))
    for r in results:
        print(json.dumps(r.__dict__))
    passed = sum(r.passed for r in results)
    print(f"{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
