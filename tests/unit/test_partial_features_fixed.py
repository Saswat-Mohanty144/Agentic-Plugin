"""P1-P10 (implementation-status audit 2026-10-29): partial features fail closed instead of inventing data.

P1/P2 compliance diagnose and dashboard: no host data source -> 503, demo data only behind HRMS_DEMO_ENDPOINTS and
labelled; P3/P4/P10a statutory tools refuse jurisdictions they do not implement; P5 tool arguments are validated
against the declared schema; P6/P7 no invented leave balance, skills or experience; P8 archetypes carry no invented
pay, validity or benefits; P9 legacy chat takes the jurisdiction from the request, never from keywords; P10 the
compliance audit tool never audits fictional employees.
"""

import asyncio

import pytest

from hrms_plugin.agents.tools import AgenticToolRegistry

REG = AgenticToolRegistry()
EMP = {"id": "E1", "basic_salary": 4000.0, "gross_salary": 9000.0}
STATUTORY = {
    "calculate_salary_structure": {"gross_or_ctc": 1200000},
    "calculate_eosb_gratuity": {"basic_wage": 5000, "tenure_years": 6},
    "audit_statutory_compliance": {"employees": [EMP]},
    "evaluate_onboarding_offer": {"candidate_name": "A", "job_title": "Engineer", "annual_salary": 900000},
    "verify_onboarding_documents": {"candidate_name": "A", "submitted_documents": {"passport": "P1"}},
    "compute_final_settlement": {"employee_id": "E1", "employee_name": "A", "basic_wage_monthly": 5000,
                                 "gross_wage_monthly": 9000, "tenure_years": 3},
}


def run(tool, **args):
    return asyncio.run(REG.execute(tool, args))


# P3 / P4 / P10a ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("tool", STATUTORY)
@pytest.mark.parametrize("jurisdiction", ["SA", "US", "GLOBAL", "in", ""])
def test_statutory_tools_refuse_unimplemented_jurisdictions(tool, jurisdiction):
    out = run(tool, jurisdiction=jurisdiction, **STATUTORY[tool])
    assert out["success"] is False and out["error"] == "JURISDICTION_NOT_SUPPORTED"
    assert out["supported"] == ["AE", "IN"]


@pytest.mark.parametrize("tool", STATUTORY)
@pytest.mark.parametrize("jurisdiction", ["IN", "AE"])
def test_statutory_tools_still_work_for_in_and_ae(tool, jurisdiction):
    out = run(tool, jurisdiction=jurisdiction, **STATUTORY[tool])
    assert "error" not in out and out.get("success") is not False


@pytest.mark.parametrize("jurisdiction", ["IN", "AE", "SA", "US"])
def test_regulation_search_keeps_all_four(jurisdiction):
    assert "error" not in run("search_labor_regulations", query="gratuity", jurisdiction=jurisdiction)


# P5 ---------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("args, problem", [
    ({"employee_id": "E1", "current_balance": 10}, "missing:requested_days"),
    ({"employee_id": "E1", "requested_days": "2", "current_balance": 10}, "type:requested_days"),
    ({"employee_id": "E1", "requested_days": True, "current_balance": 10}, "type:requested_days"),
    ({"employee_id": 7, "requested_days": 2, "current_balance": 10}, "type:employee_id"),
    ({"employee_id": "E1", "requested_days": 2, "current_balance": 10, "leave_type": "VACATION"}, "enum:leave_type"),
    ({"employee_id": "E1", "requested_days": 2, "current_balance": 10, "approve": True}, "unknown:approve"),
])
def test_arguments_are_validated_against_the_declared_schema(args, problem):
    out = run("evaluate_leave_request", **args)
    assert out["success"] is False and out["error"] == "INVALID_ARGUMENTS" and problem in out["problems"]


@pytest.mark.parametrize("args, problem", [
    ({"unpaid_work_days": 1.5}, "type:unpaid_work_days"),
    ({"unpaid_work_days": False}, "type:unpaid_work_days"),
])
def test_integer_means_integer(args, problem):
    out = run("compute_final_settlement", jurisdiction="AE", **{**STATUTORY["compute_final_settlement"], **args})
    assert out["error"] == "INVALID_ARGUMENTS" and problem in out["problems"]


def test_valid_arguments_with_ints_for_numbers_pass():
    out = run("evaluate_leave_request", employee_id="E1", requested_days=2, current_balance=10)
    assert "error" not in out


def test_every_registered_tool_declares_an_object_schema():
    for definition in REG.get_definitions():
        assert definition.parameters.get("type") == "object" and "properties" in definition.parameters


# P6 / P7 ----------------------------------------------------------------------------------------------------
def test_leave_balance_is_never_assumed():
    out = run("evaluate_leave_request", employee_id="E1", requested_days=2)
    assert out["error"] == "INVALID_ARGUMENTS" and "missing:current_balance" in out["problems"]


@pytest.mark.parametrize("missing", ["required_skills", "required_experience_years", "candidate_experience_years"])
def test_ats_criteria_are_never_assumed(missing):
    args = {"resume_text": "Python", "required_skills": ["Python"], "required_experience_years": 2,
            "candidate_experience_years": 4}
    args.pop(missing)
    out = run("score_candidate_ats", **args)
    assert out["error"] == "INVALID_ARGUMENTS" and f"missing:{missing}" in out["problems"]


def test_ats_empty_skills_list_is_rejected():
    out = run("score_candidate_ats", resume_text="Python", required_skills=[], required_experience_years=2,
              candidate_experience_years=4)
    assert out["error"] == "INVALID_ARGUMENTS" and "min_items:required_skills" in out["problems"]


# P10 --------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("args, problem", [({}, "missing:employees"), ({"employees": []}, "min_items:employees")])
def test_compliance_audit_never_invents_employees(args, problem):
    out = run("audit_statutory_compliance", jurisdiction="AE", **args)
    assert out["error"] == "INVALID_ARGUMENTS" and problem in out["problems"]


# P8 ---------------------------------------------------------------------------------------------------------
PAY_KEYS = {"salary", "minSalary", "maxSalary", "salary_range", "validTill"}


def _walk(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _assert_no_invented_terms(archetypes):
    assert archetypes
    for key, value in _walk(archetypes):
        if key in PAY_KEYS:
            assert value is None, key
        if key == "benefits_display":
            assert value == [], key


def test_archetype_tool_invents_no_pay_or_validity():
    out = REG._tool_generate_archetypes(prompt="Lead Data Engineer", location="Bangalore")
    assert out["source"] == "TEMPLATE" and out["requires_review"] is True
    _assert_no_invented_terms(out["archetypes"])


def test_archetype_route_invents_no_pay_or_validity(make_client, bearer):
    res = make_client().post("/v1/requisitions/generate-archetypes", json={"prompt": "Senior Python Engineer"},
                             headers=bearer(roles=["recruiter"]))
    body = res.json()
    assert res.status_code == 200 and body["source"] == "TEMPLATE" and body["requires_review"] is True
    _assert_no_invented_terms(body["archetypes"])


# P1 / P2 ----------------------------------------------------------------------------------------------------
COMPLIANCE_ROUTES = ["/v1/compliance/diagnose", "/api/compliance/agent/diagnose", "/v1/compliance/dashboard/live",
                     "/api/compliance/dashboard/live"]
DEMO = {"HRMS_DEMO_ENDPOINTS": "true"}


@pytest.mark.parametrize("route", COMPLIANCE_ROUTES)
@pytest.mark.parametrize("env", [None, {"HRMS_DEMO_ENDPOINTS": "false"}, {"HRMS_DEMO_ENDPOINTS": "TRUE "}])
def test_compliance_views_without_a_data_source_are_503(make_client, bearer, route, env):
    res = make_client(env=env).get(route, headers=bearer(roles=["hr_manager"]))
    assert res.status_code == 503 and res.json() == {"error": "COMPLIANCE_SOURCE_NOT_CONNECTED"}


@pytest.mark.parametrize("route", COMPLIANCE_ROUTES)
def test_demo_mode_is_labelled(make_client, bearer, route):
    res = make_client(env=DEMO).get(route, headers=bearer(roles=["hr_manager"]))
    assert res.status_code == 200 and res.json()["data_source"] == "DEMO_SAMPLE"


@pytest.mark.parametrize("jurisdiction", ["SA", "US", "XX", "ae"])
def test_diagnose_never_falls_back_to_uae(make_client, bearer, jurisdiction):
    res = make_client(env=DEMO).get(f"/v1/compliance/diagnose?jurisdiction={jurisdiction}",
                                    headers=bearer(roles=["hr_manager"]))
    assert res.status_code == 422 and res.json()["error"] == "JURISDICTION_NOT_SUPPORTED"


@pytest.mark.parametrize("jurisdiction", ["AE", "IN"])
def test_diagnose_demo_reports_the_requested_jurisdiction(make_client, bearer, jurisdiction):
    res = make_client(env=DEMO).get(f"/v1/compliance/diagnose?jurisdiction={jurisdiction}",
                                    headers=bearer(roles=["hr_manager"]))
    assert res.status_code == 200 and res.json()["jurisdiction"] == jurisdiction


def test_demo_flag_does_not_bypass_permissions(make_client, bearer):
    res = make_client(env=DEMO).get("/v1/compliance/diagnose", headers=bearer(roles=["staff"]))
    assert res.status_code == 403


# P9 ---------------------------------------------------------------------------------------------------------
class _Spy:
    def __init__(self):
        self.calls = []

    def process_message(self, **kw):
        from hrms_plugin.agents.supervisor import AgentResponse, UserIntent

        self.calls.append(kw)
        return AgentResponse(intent=UserIntent.PAYROLL_EOSB, routed_agent="payroll", reply_text="ok",
                             confidence_score=1.0)


@pytest.mark.parametrize("body", [{"message": "uae eosb please"}, {"message": "hi", "jurisdiction": "XX"},
                                  {"message": "hi", "jurisdiction": "ae"}, {"jurisdiction": "AE"},
                                  {"message": 5, "jurisdiction": "AE"}])
def test_legacy_chat_rejects_missing_or_invalid_input(make_client, bearer, body):
    spy = _Spy()
    res = make_client(supervisor_agent=spy).post("/api/compliance/agent/chat", json=body,
                                                 headers=bearer(roles=["staff"]))
    assert res.status_code == 422 and spy.calls == []


@pytest.mark.parametrize("message, jurisdiction", [("uae eosb in aed", "IN"), ("india pf", "AE"), ("hi", "SA")])
def test_legacy_chat_uses_the_requested_jurisdiction_not_keywords(make_client, bearer, message, jurisdiction):
    spy = _Spy()
    res = make_client(supervisor_agent=spy).post("/api/compliance/agent/chat",
                                                 json={"message": message, "jurisdiction": jurisdiction},
                                                 headers=bearer(roles=["staff"]))
    assert res.status_code == 200 and spy.calls[0]["jurisdiction"] == jurisdiction
