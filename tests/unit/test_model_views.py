"""Token waste #4: the model gets a compact view of UI-heavy tool output; the UI still gets everything.

Archetypes -> name and badge per archetype; compliance audit -> counts plus each violation's id, severity, entity,
summary, citation and recommendation. Errors and tools without a view are passed through unchanged.
"""

import asyncio
import json

from hrms_plugin.agents.orchestrator import AgenticOrchestrator, tool_result_content
from hrms_plugin.agents.tools import AgenticToolRegistry
from hrms_plugin.security.rbac import AuthContext, UserRole
from tests.support.scripted_llm import final, scripted_gateway, tool_call

REG = AgenticToolRegistry()
ADMIN = AuthContext(user_id="u", tenant_id="acme", roles=frozenset({UserRole.SUPER_ADMIN}))
EMP = [{"id": "E1", "basic_salary": 4500, "gross_salary": 12000, "probation_days": 210, "weekly_hours": 54,
        "uae_wps_registered": False}]


def test_archetype_view_is_names_and_badges_only():
    full = REG._tool_generate_archetypes(prompt="Lead Data Engineer", location="Bangalore")
    view = REG.model_view("generate_requisition_archetypes", full)
    assert view["archetypes"] == [{"name": a["name"], "badge": a["badge"]} for a in full["archetypes"]]
    assert "suggested_actions" not in view and view["requires_review"] is True
    assert len(tool_result_content(view)) * 5 < len(tool_result_content(full))


def test_audit_view_keeps_what_the_answer_needs():
    full = asyncio.run(REG.execute("audit_statutory_compliance", {"jurisdiction": "AE", "employees": EMP}))
    view = REG.model_view("audit_statutory_compliance", full)
    assert view["violations_count"] == full["violations_count"] > 0
    for v_full, v in zip(full["violations"], view["violations"]):
        assert v == {k: v_full[k] for k in ("violation_id", "severity", "entity_id", "summary",
                                            "statutory_citation", "remediation_recommendation")}
    assert "remediation_patch" not in json.dumps(view) and "detailed_findings" not in json.dumps(view)


def test_errors_and_tools_without_a_view_pass_through():
    err = {"success": False, "error": "INVALID_ARGUMENTS", "problems": ["missing:x"]}
    assert REG.model_view("generate_requisition_archetypes", err) == err
    out = {"citations": ["a"], "results": [1]}
    assert REG.model_view("search_labor_regulations", out) == out


def test_model_gets_the_view_and_the_ui_gets_the_full_output():
    gateway, client = scripted_gateway([tool_call("generate_requisition_archetypes", prompt="Data Engineer"),
                                        final("done")])
    orch = AgenticOrchestrator(llm_gateway=gateway, tool_registry=AgenticToolRegistry())
    resp = asyncio.run(orch.run(user_message="create a requisition", auth=ADMIN))
    sent = [m.content for m in client.calls[1]["messages"] if m.role == "tool"][0]
    assert "jobData" not in sent and "suggested_actions" not in sent and "badge" in sent
    assert "suggested_actions" in resp.structured_data and resp.structured_data["archetypes"][0]["jobData"]
