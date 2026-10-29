"""QG: the eval runner reports tokens per case and whether every citation in the answer is grounded.

Citation check: each "Article N" / "Section N" the final answer claims must appear in some tool output of that run
(the regulation search runs for real; host-touching tools stay stubbed). Tokens come from provider-reported usage;
no usage reported -> None, never a guess.
"""

import asyncio

from hrms_plugin.ai.llm import LLMResponse
from hrms_plugin.evals.runner import EvalCase, run_case, summarise
from hrms_plugin.security.rbac import UserRole
from tests.support.scripted_llm import scripted_gateway, tool_call

CASE = EvalCase(id="c1", prompt="Which article governs UAE gratuity?", jurisdiction="AE", role=UserRole.HR_ADMIN,
                expect_first_tool="search_labor_regulations")


def reply(text, usage=None):
    return LLMResponse(content=text, finish_reason="stop", model="s", provider="s", usage=usage or {})


def search_call(usage=None):
    r = tool_call("search_labor_regulations", query="end of service gratuity")
    r.usage = usage or {}
    return r


def run(*responses):
    gateway, _ = scripted_gateway(list(responses))
    return asyncio.run(run_case(CASE, gateway))


def test_grounded_citation_passes():
    result = run(search_call(), reply("Gratuity is governed by Article 51."))
    assert result.passed is True and result.citations_ok is True and result.ungrounded_citations == []


def test_ungrounded_citation_fails_the_case():
    result = run(search_call(), reply("See Article 51 and Article 99 and Section 12."))
    assert result.passed is False and result.citations_ok is False
    assert result.ungrounded_citations == ["Article 99", "Section 12"] and "ungrounded" in result.reason


def test_citation_without_any_tool_call_is_ungrounded():
    case = EvalCase(id="c2", prompt="hi", jurisdiction="AE", role=UserRole.HR_ADMIN, expect_first_tool=None)
    gateway, _ = scripted_gateway([reply("Per Article 51 you get gratuity.")])
    result = asyncio.run(run_case(case, gateway))
    assert result.passed is False and result.ungrounded_citations == ["Article 51"]


def test_tokens_are_summed_from_reported_usage():
    result = run(search_call({"input_tokens": 900, "output_tokens": 40, "cache_read_input_tokens": 800}),
                 reply("Article 51.", {"input_tokens": 1000, "output_tokens": 60}))
    assert result.tokens == 2800


def test_tokens_unknown_when_provider_reports_none():
    assert run(search_call(), reply("Article 51.")).tokens is None


def test_summary_reports_pass_rate_and_median_tokens():
    a = run(search_call({"input_tokens": 100, "output_tokens": 0}), reply("Article 51.", {"input_tokens": 100}))
    b = run(search_call({"input_tokens": 300, "output_tokens": 0}), reply("Article 99."))
    c = run(search_call(), reply("Article 51."))
    s = summarise([a, b, c])
    assert s == {"cases": 3, "passed": 2, "median_tokens": 250, "cases_without_usage": 1,
                 "ungrounded_citation_cases": 1}
