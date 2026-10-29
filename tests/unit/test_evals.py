"""Eval harness and prompt-prefix cost guards (piece H8)."""

import pytest
import yaml

from hrms_plugin.evals import runner
from hrms_plugin.evals.runner import EvalCase, load_cases, prefix_chars, run_case, score
from hrms_plugin.security.rbac import UserRole
from tests.support.scripted_llm import final, tool_call

PF = EvalCase("pf", "pf please", "IN", UserRole.PAYROLL_ADMIN, "calculate_pf_contribution",
              ("calculate_salary_structure",))
NONE = EvalCase("none", "hi", "IN", UserRole.EMPLOYEE, None, ("calculate_pf_contribution",))


def test_shipped_cases_load_and_cover_positive_and_negative_routing():
    cases = load_cases()
    assert len(cases) >= 8
    assert any(c.expect_first_tool is None and c.forbid_tools for c in cases)  # must-not-call cases exist
    assert len({c.role for c in cases}) >= 4


@pytest.mark.parametrize(
    "mutate,message",
    [
        (lambda d: d.pop("version"), "version"),
        (lambda d: d["cases"].append(dict(d["cases"][0])), "duplicate"),
        (lambda d: d["cases"][0].update(expect_first_tool="no_such_tool"), "unknown tool"),
        (lambda d: d["cases"][0].update(forbid_tools=["no_such_tool"]), "unknown tool"),
    ],
)
def test_invalid_case_files_are_rejected(tmp_path, mutate, message):
    data = yaml.safe_load(runner.CASES_PATH.read_text())
    mutate(data)
    path = tmp_path / "cases.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match=message):
        load_cases(path)


def test_unknown_role_is_rejected(tmp_path):
    data = yaml.safe_load(runner.CASES_PATH.read_text())
    data["cases"][0]["role"] = "GOD"
    path = tmp_path / "cases.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(KeyError):
        load_cases(path)


@pytest.mark.parametrize(
    "case,called,passed",
    [
        (PF, ["calculate_pf_contribution"], True),
        (PF, ["calculate_pf_contribution", "search_labor_regulations"], True),
        (PF, ["search_labor_regulations", "calculate_pf_contribution"], False),  # wrong first tool
        (PF, ["calculate_pf_contribution", "calculate_salary_structure"], False),  # forbidden later
        (PF, [], False),
        (NONE, [], True),
        (NONE, ["search_labor_regulations"], False),
        (NONE, ["calculate_pf_contribution"], False),
    ],
)
def test_scorer(case, called, passed):
    assert score(case, called).passed is passed


async def test_harness_passes_correct_routing_without_touching_a_host(scripted):
    gateway, _ = scripted(tool_call("calculate_pf_contribution", employee_id="HR-EMP-00001", period="2026-09"),
                          final("done"))
    result = await run_case(PF, gateway)
    assert result.passed and result.tools_called == ["calculate_pf_contribution"]


async def test_harness_fails_wrong_routing(scripted):
    gateway, _ = scripted(tool_call("calculate_salary_structure", ctc=1), final("done"))
    assert not (await run_case(PF, gateway)).passed


async def test_rbac_refused_call_still_counts_against_the_model(scripted):
    gateway, _ = scripted(tool_call("calculate_pf_contribution", employee_id="HR-EMP-00002", period="2026-09"),
                          final("no"))
    result = await run_case(NONE, gateway)
    assert not result.passed and "forbidden" in result.reason


# Cost guards on the cacheable prefix ------------------------------------------------------------------------

PREFIX_CEILING_CHARS = 9000  # measured 8 302 for full-access roles on 2026-10-29; growth needs a deliberate bump


def test_largest_prefix_stays_under_ceiling():
    assert max(prefix_chars(r) for r in UserRole) <= PREFIX_CEILING_CHARS


def test_narrower_roles_send_smaller_prefixes():
    assert prefix_chars(UserRole.EMPLOYEE) < prefix_chars(UserRole.PAYROLL_ADMIN) < prefix_chars(UserRole.SUPER_ADMIN)


# CLI ------------------------------------------------------------------------------------------------------------


def test_cli_offline_is_a_no_op():
    assert runner.main([]) == 0


def test_cli_live_refuses_without_approval(monkeypatch):
    monkeypatch.delenv("HRMS_EVAL_LIVE_APPROVED", raising=False)

    def boom(*_a, **_k):
        raise AssertionError("must not build a gateway")

    monkeypatch.setattr(runner, "LLMGateway", boom)
    assert runner.main(["--live"]) == 2


def test_cli_live_requires_exact_approval_value(monkeypatch):
    monkeypatch.setenv("HRMS_EVAL_LIVE_APPROVED", "yes")
    monkeypatch.setattr(runner, "LLMGateway", lambda: (_ for _ in ()).throw(AssertionError("no")))
    assert runner.main(["--live"]) == 2
