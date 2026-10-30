"""Tests for Decision D1 (Gap A): non-agentic supervisor refuses missing inputs without inventing values."""

import re

import pytest

from hrms_plugin.agents.supervisor import SupervisorAgent, UserIntent
from hrms_plugin.rag.store import Jurisdiction
from hrms_plugin.security.rbac import AuthContext, UserRole

ADMIN = AuthContext(user_id="test-admin", tenant_id="acme", roles=frozenset({UserRole.SUPER_ADMIN}))


@pytest.fixture
def supervisor():
    return SupervisorAgent()


def _has_no_figures(text: str) -> bool:
    """Return True if text contains no numeric digits / figures."""
    return re.search(r"\d", text) is None


# 1. Statutory Salary Structuring
def test_payroll_structure_missing_inputs(supervisor):
    # Empty context
    resp = supervisor.process_message(
        message="Calculate CTC breakdown and net take home",
        jurisdiction=Jurisdiction.INDIA,
        auth=ADMIN,
        context={},
    )
    assert resp.intent == UserIntent.PAYROLL_STRUCTURE
    assert resp.routed_agent == "StatutoryPayrollAgent"
    assert resp.confidence_score == 0.0
    assert resp.structured_data == {"error": "MISSING_INPUT", "missing": ["annual_ctc"]}
    assert _has_no_figures(resp.reply_text)
    assert "1200000" not in resp.reply_text

    # None context value
    resp_none = supervisor.process_message(
        message="Calculate salary structure",
        jurisdiction=Jurisdiction.INDIA,
        auth=ADMIN,
        context={"annual_ctc": None},
    )
    assert resp_none.structured_data == {"error": "MISSING_INPUT", "missing": ["annual_ctc"]}

    # UAE jurisdiction with empty context
    resp_uae_empty = supervisor.process_message(
        message="Calculate salary structure for UAE employee",
        jurisdiction=Jurisdiction.UAE,
        auth=ADMIN,
        context={},
    )
    assert resp_uae_empty.confidence_score == 0.0
    assert resp_uae_empty.structured_data == {"error": "MISSING_INPUT", "missing": ["annual_ctc"]}
    assert _has_no_figures(resp_uae_empty.reply_text)

    # Supplied context keeps working exactly as before
    resp_in = supervisor.process_message(
        message="Calculate CTC breakdown and net take home",
        jurisdiction=Jurisdiction.INDIA,
        auth=ADMIN,
        context={"annual_ctc": 1200000.0},
    )
    assert resp_in.confidence_score > 0.0
    assert resp_in.structured_data["annual_ctc"] == 1200000.0
    assert resp_in.structured_data["net_take_home"] == 88276.92

    resp_uae = supervisor.process_message(
        message="Calculate salary structure for UAE employee",
        jurisdiction=Jurisdiction.UAE,
        auth=ADMIN,
        context={"ctc": 240000.0},
    )
    assert resp_uae.confidence_score > 0.0
    assert resp_uae.structured_data["basic_wage"] == 12000.0


# 2. UAE EOSB Calculation
def test_payroll_eosb_missing_inputs(supervisor):
    # Empty context
    resp = supervisor.process_message(
        message="Calculate UAE end of service severance gratuity",
        jurisdiction=Jurisdiction.UAE,
        auth=ADMIN,
        context={},
    )
    assert resp.intent == UserIntent.PAYROLL_EOSB
    assert resp.routed_agent == "StatutoryPayrollAgent"
    assert resp.confidence_score == 0.0
    assert resp.structured_data == {"error": "MISSING_INPUT", "missing": ["basic_wage_monthly", "tenure_years"]}
    assert _has_no_figures(resp.reply_text)
    assert "15000" not in resp.reply_text
    assert "3.5" not in resp.reply_text

    # Partial context: missing tenure
    resp_no_tenure = supervisor.process_message(
        message="Calculate UAE end of service gratuity",
        jurisdiction=Jurisdiction.UAE,
        auth=ADMIN,
        context={"basic_wage_monthly": 10000.0},
    )
    assert resp_no_tenure.confidence_score == 0.0
    assert resp_no_tenure.structured_data == {"error": "MISSING_INPUT", "missing": ["tenure_years"]}
    assert _has_no_figures(resp_no_tenure.reply_text)

    # Partial context: missing basic wage
    resp_no_basic = supervisor.process_message(
        message="Calculate UAE end of service gratuity",
        jurisdiction=Jurisdiction.UAE,
        auth=ADMIN,
        context={"tenure_years": 4.0},
    )
    assert resp_no_basic.confidence_score == 0.0
    assert resp_no_basic.structured_data == {"error": "MISSING_INPUT", "missing": ["basic_wage_monthly"]}
    assert _has_no_figures(resp_no_basic.reply_text)

    # Supplied context keeps working exactly as before
    resp_ok = supervisor.process_message(
        message="Calculate UAE end of service severance gratuity",
        jurisdiction=Jurisdiction.UAE,
        auth=ADMIN,
        context={"basic_wage_monthly": 10000.0, "tenure_years": 4.0},
    )
    assert resp_ok.confidence_score > 0.0
    assert resp_ok.structured_data["total_eosb_gratuity"] == 28000.0


# 3. Leave Application
def test_leave_apply_missing_inputs(supervisor):
    resp = supervisor.process_message(
        message="Apply leave for vacation request",
        jurisdiction=Jurisdiction.INDIA,
        auth=ADMIN,
        context={},
    )
    assert resp.intent == UserIntent.LEAVE_APPLY
    assert resp.routed_agent == "LeaveAttendanceAgent"
    assert resp.confidence_score == 0.0
    assert resp.structured_data == {
        "error": "MISSING_INPUT",
        "missing": ["employee_id", "leave_type", "requested_days", "current_balance"],
    }
    assert _has_no_figures(resp.reply_text)

    # Supplied context works
    resp_ok = supervisor.process_message(
        message="Apply leave for vacation request",
        jurisdiction=Jurisdiction.INDIA,
        auth=ADMIN,
        context={"employee_id": "EMP-001", "requested_days": 2.0, "current_balance": 10.0, "leave_type": "ANNUAL"},
    )
    assert resp_ok.confidence_score > 0.0
    assert resp_ok.structured_data["is_approved"] is True
    assert resp_ok.structured_data["remaining_balance"] == 8.0


# 4. Impossible Travel Anomaly
def test_attendance_anomaly_missing_inputs(supervisor):
    resp = supervisor.process_message(
        message="Check impossible travel punch anomaly",
        jurisdiction=Jurisdiction.INDIA,
        auth=ADMIN,
        context={},
    )
    assert resp.intent == UserIntent.ATTENDANCE_ANOMALY
    assert resp.routed_agent == "LeaveAttendanceAgent"
    assert resp.confidence_score == 0.0
    assert resp.structured_data == {"error": "MISSING_INPUT", "missing": ["punch1", "punch2"]}
    assert _has_no_figures(resp.reply_text)

    # Supplied context works
    p1 = {"lat": 19.0760, "lng": 72.8777, "timestamp": "2026-09-24T09:00:00Z"}
    p2 = {"lat": 25.2048, "lng": 55.2708, "timestamp": "2026-09-24T10:00:00Z"}
    resp_ok = supervisor.process_message(
        message="Check impossible travel punch anomaly",
        jurisdiction=Jurisdiction.INDIA,
        auth=ADMIN,
        context={"punch1": p1, "punch2": p2},
    )
    assert resp_ok.confidence_score > 0.0
    assert resp_ok.structured_data["is_anomaly"] is True


# 5. Recruitment ATS Scoring
def test_recruitment_ats_missing_inputs(supervisor):
    resp = supervisor.process_message(
        message="Match candidate resume and give ats score",
        jurisdiction=Jurisdiction.INDIA,
        auth=ADMIN,
        context={},
    )
    assert resp.intent == UserIntent.RECRUITMENT_ATS
    assert resp.routed_agent == "RecruitmentAgent"
    assert resp.confidence_score == 0.0
    assert resp.structured_data == {
        "error": "MISSING_INPUT",
        "missing": ["resume_text", "required_skills", "required_experience_years", "candidate_experience_years"],
    }
    assert _has_no_figures(resp.reply_text)

    # Supplied context works
    resp_ok = supervisor.process_message(
        message="Match candidate resume and give ats score",
        jurisdiction=Jurisdiction.INDIA,
        auth=ADMIN,
        context={
            "resume_text": "Experienced Python and FastAPI backend developer with Docker and PostgreSQL.",
            "required_skills": ["Python", "FastAPI"],
            "required_experience_years": 3.0,
            "candidate_experience_years": 4.0,
        },
    )
    assert resp_ok.confidence_score > 0.0
    assert "composite_score" in resp_ok.structured_data


# 6. Onboarding Specialist (KYC, Provisioning, Offer)
def test_onboarding_missing_inputs(supervisor):
    # 6a. KYC Document Verification
    resp_docs = supervisor.process_message(
        message="Verify kyc documents for new hire",
        jurisdiction=Jurisdiction.INDIA,
        auth=ADMIN,
        context={},
    )
    assert resp_docs.intent == UserIntent.ONBOARDING
    assert resp_docs.routed_agent == "OnboardingAgent"
    assert resp_docs.confidence_score == 0.0
    assert resp_docs.structured_data == {"error": "MISSING_INPUT", "missing": ["candidate_name", "documents"]}
    assert _has_no_figures(resp_docs.reply_text)

    # 6b. Provisioning Plan
    resp_prov = supervisor.process_message(
        message="Generate provision plan for new hire",
        jurisdiction=Jurisdiction.INDIA,
        auth=ADMIN,
        context={},
    )
    assert resp_prov.intent == UserIntent.ONBOARDING
    assert resp_prov.confidence_score == 0.0
    assert resp_prov.structured_data == {
        "error": "MISSING_INPUT",
        "missing": ["candidate_name", "employee_id", "role"],
    }
    assert _has_no_figures(resp_prov.reply_text)

    # 6c. Offer Evaluation
    resp_offer = supervisor.process_message(
        message="Evaluate offer letter for new hire candidate",
        jurisdiction=Jurisdiction.INDIA,
        auth=ADMIN,
        context={},
    )
    assert resp_offer.intent == UserIntent.ONBOARDING
    assert resp_offer.confidence_score == 0.0
    assert resp_offer.structured_data == {
        "error": "MISSING_INPUT",
        "missing": ["candidate_name", "job_title", "annual_salary"],
    }
    assert _has_no_figures(resp_offer.reply_text)

    # Supplied offer context works
    resp_offer_ok = supervisor.process_message(
        message="Evaluate offer letter for new hire candidate",
        jurisdiction=Jurisdiction.INDIA,
        auth=ADMIN,
        context={
            "candidate_name": "Deepak Patel",
            "job_title": "Product Designer",
            "annual_salary": 1400000.0,
            "band_min": 1000000.0,
            "band_max": 1800000.0,
        },
    )
    assert resp_offer_ok.confidence_score > 0.0
    assert resp_offer_ok.structured_data["within_band"] is True


# 7. Offboarding & Final Settlement (Timeline, Clearance, Calculation)
def test_offboarding_missing_inputs(supervisor):
    # 7a. Article 53 settlement timeline
    resp_timeline = supervisor.process_message(
        message="Check Article 53 statutory settlement timeline deadline",
        jurisdiction=Jurisdiction.UAE,
        auth=ADMIN,
        context={},
    )
    assert resp_timeline.intent == UserIntent.OFFBOARDING_FNF
    assert resp_timeline.routed_agent == "OffboardingAgent"
    assert resp_timeline.confidence_score == 0.0
    assert resp_timeline.structured_data == {
        "error": "MISSING_INPUT",
        "missing": ["employee_id", "employee_name", "last_working_day"],
    }
    assert _has_no_figures(resp_timeline.reply_text)

    # 7b. Exit clearance
    resp_clr = supervisor.process_message(
        message="Process exit clearance for offboard employee",
        jurisdiction=Jurisdiction.UAE,
        auth=ADMIN,
        context={},
    )
    assert resp_clr.intent == UserIntent.OFFBOARDING_FNF
    assert resp_clr.confidence_score == 0.0
    assert resp_clr.structured_data == {
        "error": "MISSING_INPUT",
        "missing": ["employee_id", "employee_name", "returned_assets", "pending_assets", "finance_cleared"],
    }
    assert _has_no_figures(resp_clr.reply_text)

    # 7c. Final settlement calculation
    resp_fnf = supervisor.process_message(
        message="Calculate final settlement fnf for employee resignation",
        jurisdiction=Jurisdiction.UAE,
        auth=ADMIN,
        context={},
    )
    assert resp_fnf.intent == UserIntent.OFFBOARDING_FNF
    assert resp_fnf.confidence_score == 0.0
    assert resp_fnf.structured_data == {
        "error": "MISSING_INPUT",
        "missing": [
            "employee_id",
            "employee_name",
            "basic_wage_monthly",
            "gross_wage_monthly",
            "tenure_years",
            "unused_leave_days",
            "unpaid_work_days",
        ],
    }
    assert _has_no_figures(resp_fnf.reply_text)

    # Supplied final settlement context works
    resp_fnf_ok = supervisor.process_message(
        message="Calculate final settlement fnf for employee resignation",
        jurisdiction=Jurisdiction.UAE,
        auth=ADMIN,
        context={
            "employee_id": "EMP-01",
            "employee_name": "Tariq",
            "basic_wage_monthly": 12000.0,
            "gross_wage_monthly": 20000.0,
            "tenure_years": 3.0,
            "unused_leave_days": 0.0,
            "unpaid_work_days": 0,
        },
    )
    assert resp_fnf_ok.confidence_score > 0.0
    assert "total_net_payable" in resp_fnf_ok.structured_data


# 8. Travel & Expense Claim Audit
def test_travel_expense_missing_inputs(supervisor):
    resp = supervisor.process_message(
        message="Audit travel expense claim and reimbursement claim",
        jurisdiction=Jurisdiction.INDIA,
        auth=ADMIN,
        context={},
    )
    assert resp.intent == UserIntent.TRAVEL_EXPENSE
    assert resp.routed_agent == "TravelExpenseAgent"
    assert resp.confidence_score == 0.0
    assert resp.structured_data == {
        "error": "MISSING_INPUT",
        "missing": ["claim_id", "employee_id", "expenses", "tier", "has_approved_travel_request"],
    }
    assert _has_no_figures(resp.reply_text)

    # Supplied context works
    resp_ok = supervisor.process_message(
        message="Audit travel expense claim and reimbursement claim",
        jurisdiction=Jurisdiction.INDIA,
        auth=ADMIN,
        context={
            "claim_id": "CLM-55",
            "employee_id": "EMP-TRV",
            "expenses": [{"id": "EXP-1", "category": "MEAL", "amount": "1500.00", "has_receipt": True}],
            "tier": "METRO",
            "has_approved_travel_request": True,
        },
    )
    assert resp_ok.confidence_score > 0.0
    assert resp_ok.structured_data["is_fully_approved"] is True


# 9. No reply text contains 1200000, 15000, or 3.5 when context is empty
def test_no_reply_contains_invented_figures_when_context_is_empty(supervisor):
    queries = [
        ("Calculate salary structure for employee", Jurisdiction.INDIA),
        ("Calculate salary structure for employee", Jurisdiction.UAE),
        ("Calculate UAE end of service gratuity", Jurisdiction.UAE),
        ("Run statutory compliance audit for employees", Jurisdiction.INDIA),
        ("Apply leave for 3 days", Jurisdiction.INDIA),
        ("Check impossible travel punch anomaly", Jurisdiction.INDIA),
        ("Match candidate resume and give ats score", Jurisdiction.INDIA),
        ("Verify kyc documents", Jurisdiction.INDIA),
        ("Generate provision plan", Jurisdiction.INDIA),
        ("Evaluate offer letter", Jurisdiction.INDIA),
        ("Check Article 53 settlement timeline", Jurisdiction.UAE),
        ("Process exit clearance", Jurisdiction.UAE),
        ("Calculate final settlement fnf", Jurisdiction.UAE),
        ("Audit travel expense claim", Jurisdiction.INDIA),
    ]

    for msg, jur in queries:
        resp = supervisor.process_message(message=msg, jurisdiction=jur, auth=ADMIN, context={})
        assert "1200000" not in resp.reply_text, f"1200000 found in reply for {msg}: {resp.reply_text}"
        assert "15000" not in resp.reply_text, f"15000 found in reply for {msg}: {resp.reply_text}"
        assert "3.5" not in resp.reply_text, f"3.5 found in reply for {msg}: {resp.reply_text}"
        assert "Engineering" not in resp.reply_text, f"Engineering found in reply for {msg}: {resp.reply_text}"
        if resp.structured_data and resp.structured_data.get("error") == "MISSING_INPUT":
            assert _has_no_figures(resp.reply_text), f"Figures found in missing input reply: {resp.reply_text}"


# 10. HTTP endpoint POST /v1/chat with agentic=false
def test_http_chat_non_agentic_missing_input_and_supplied(make_client, bearer):
    client = make_client()
    headers = bearer(roles=["hr_manager"])

    # Missing input on payroll structure -> 200 with MISSING_INPUT
    res_missing = client.post(
        "/v1/chat",
        headers=headers,
        json={"message": "Calculate salary structure", "jurisdiction": "IN", "agentic": False, "context": {}},
    )
    assert res_missing.status_code == 200
    data_missing = res_missing.json()
    assert data_missing["intent"] == "PAYROLL_STRUCTURE"
    assert data_missing["confidence_score"] == 0.0
    assert data_missing["structured_data"] == {"error": "MISSING_INPUT", "missing": ["annual_ctc"]}
    assert _has_no_figures(data_missing["reply_text"])
    assert "1200000" not in data_missing["reply_text"]

    # Supplied input on payroll structure -> 200 with calculation
    res_supplied = client.post(
        "/v1/chat",
        headers=headers,
        json={
            "message": "Calculate salary structure",
            "jurisdiction": "IN",
            "agentic": False,
            "context": {"annual_ctc": 1800000.0},
        },
    )
    assert res_supplied.status_code == 200
    data_supplied = res_supplied.json()
    assert data_supplied["confidence_score"] > 0.0
    assert data_supplied["structured_data"]["annual_ctc"] == 1800000.0

    # Missing input on UAE EOSB -> 200 with MISSING_INPUT
    res_eosb = client.post(
        "/v1/chat",
        headers=headers,
        json={
            "message": "Calculate UAE end of service gratuity",
            "jurisdiction": "AE",
            "agentic": False,
            "context": {},
        },
    )
    assert res_eosb.status_code == 200
    data_eosb = res_eosb.json()
    assert data_eosb["confidence_score"] == 0.0
    assert data_eosb["structured_data"] == {
        "error": "MISSING_INPUT",
        "missing": ["basic_wage_monthly", "tenure_years"],
    }
    assert _has_no_figures(data_eosb["reply_text"])
    assert "15000" not in data_eosb["reply_text"]
    assert "3.5" not in data_eosb["reply_text"]


# 11. Compliance Audit Missing Inputs
def test_compliance_audit_missing_inputs(supervisor):
    # Empty context
    resp_empty = supervisor.process_message(
        message="Run statutory compliance audit for employees",
        jurisdiction=Jurisdiction.INDIA,
        auth=ADMIN,
        context={},
    )
    assert resp_empty.intent == UserIntent.COMPLIANCE_AUDIT
    assert resp_empty.routed_agent == "ComplianceAgent"
    assert resp_empty.confidence_score == 0.0
    assert resp_empty.structured_data == {"error": "MISSING_INPUT", "missing": ["employees"]}
    assert _has_no_figures(resp_empty.reply_text)

    # Empty list
    resp_list = supervisor.process_message(
        message="Run statutory compliance audit for employees",
        jurisdiction=Jurisdiction.INDIA,
        auth=ADMIN,
        context={"employees": []},
    )
    assert resp_list.confidence_score == 0.0
    assert resp_list.structured_data == {"error": "MISSING_INPUT", "missing": ["employees"]}

    # None value
    resp_none = supervisor.process_message(
        message="Run statutory compliance audit for employees",
        jurisdiction=Jurisdiction.INDIA,
        auth=ADMIN,
        context={"employees": None},
    )
    assert resp_none.confidence_score == 0.0
    assert resp_none.structured_data == {"error": "MISSING_INPUT", "missing": ["employees"]}

    # Supplied employee records
    resp_ok = supervisor.process_message(
        message="Run statutory compliance audit for employees",
        jurisdiction=Jurisdiction.INDIA,
        auth=ADMIN,
        context={"employees": [{"employee_id": "EMP-01", "basic_salary": 25000.0, "gross_salary": 50000.0}]},
    )
    assert resp_ok.confidence_score > 0.0
    assert "is_compliant" in resp_ok.structured_data


# 12. Leave Apply Leave Type Required
def test_leave_apply_leave_type_required(supervisor):
    # Missing leave_type when others provided
    resp_no_lt = supervisor.process_message(
        message="Apply leave for vacation request",
        jurisdiction=Jurisdiction.INDIA,
        auth=ADMIN,
        context={"employee_id": "EMP-001", "requested_days": 2.0, "current_balance": 10.0},
    )
    assert resp_no_lt.confidence_score == 0.0
    assert resp_no_lt.structured_data == {"error": "MISSING_INPUT", "missing": ["leave_type"]}
    assert _has_no_figures(resp_no_lt.reply_text)

    # leave_type is None
    resp_none = supervisor.process_message(
        message="Apply leave for vacation request",
        jurisdiction=Jurisdiction.INDIA,
        auth=ADMIN,
        context={"employee_id": "EMP-001", "requested_days": 2.0, "current_balance": 10.0, "leave_type": None},
    )
    assert resp_none.confidence_score == 0.0
    assert resp_none.structured_data == {"error": "MISSING_INPUT", "missing": ["leave_type"]}

    # Supplied leave_type works
    resp_ok = supervisor.process_message(
        message="Apply leave for vacation request",
        jurisdiction=Jurisdiction.INDIA,
        auth=ADMIN,
        context={"employee_id": "EMP-001", "requested_days": 2.0, "current_balance": 10.0, "leave_type": "CASUAL"},
    )
    assert resp_ok.confidence_score > 0.0
    assert resp_ok.structured_data["leave_type"] == "CASUAL"


# 13. Exit Clearance Invented Defaults Removed
def test_exit_clearance_invented_defaults_removed(supervisor):
    # Missing finance_cleared
    resp_no_fin = supervisor.process_message(
        message="Process exit clearance for offboard employee",
        jurisdiction=Jurisdiction.UAE,
        auth=ADMIN,
        context={"employee_id": "EMP-01", "employee_name": "Sara", "returned_assets": [], "pending_assets": []},
    )
    assert resp_no_fin.confidence_score == 0.0
    assert resp_no_fin.structured_data == {"error": "MISSING_INPUT", "missing": ["finance_cleared"]}

    # Missing returned_assets
    resp_no_ret = supervisor.process_message(
        message="Process exit clearance for offboard employee",
        jurisdiction=Jurisdiction.UAE,
        auth=ADMIN,
        context={"employee_id": "EMP-01", "employee_name": "Sara", "pending_assets": [], "finance_cleared": True},
    )
    assert resp_no_ret.confidence_score == 0.0
    assert resp_no_ret.structured_data == {"error": "MISSING_INPUT", "missing": ["returned_assets"]}

    # Missing pending_assets
    resp_no_pend = supervisor.process_message(
        message="Process exit clearance for offboard employee",
        jurisdiction=Jurisdiction.UAE,
        auth=ADMIN,
        context={"employee_id": "EMP-01", "employee_name": "Sara", "returned_assets": [], "finance_cleared": True},
    )
    assert resp_no_pend.confidence_score == 0.0
    assert resp_no_pend.structured_data == {"error": "MISSING_INPUT", "missing": ["pending_assets"]}

    # Boundary: finance_cleared=False is a valid boolean value
    resp_fin_false = supervisor.process_message(
        message="Process exit clearance for offboard employee",
        jurisdiction=Jurisdiction.UAE,
        auth=ADMIN,
        context={"employee_id": "EMP-01", "employee_name": "Sara", "returned_assets": [], "pending_assets": [],
                 "finance_cleared": False},
    )
    assert resp_fin_false.confidence_score > 0.0
    assert resp_fin_false.structured_data["is_fully_cleared"] is False

    # Department optional: when omitted, never invent "Engineering"
    resp_no_dept = supervisor.process_message(
        message="Process exit clearance for offboard employee",
        jurisdiction=Jurisdiction.UAE,
        auth=ADMIN,
        context={
            "employee_id": "EMP-01",
            "employee_name": "Sara",
            "returned_assets": ["Badge"],
            "pending_assets": [],
            "finance_cleared": True,
        },
    )
    assert resp_no_dept.confidence_score > 0.0
    assert resp_no_dept.structured_data["is_fully_cleared"] is True
    assert "Engineering" not in (resp_no_dept.structured_data.get("relieving_letter_draft") or "")

    # Department supplied: included in relieving letter
    resp_dept = supervisor.process_message(
        message="Process exit clearance for offboard employee",
        jurisdiction=Jurisdiction.UAE,
        auth=ADMIN,
        context={
            "employee_id": "EMP-01",
            "employee_name": "Sara",
            "returned_assets": ["Badge"],
            "pending_assets": [],
            "finance_cleared": True,
            "department": "Finance",
        },
    )
    assert resp_dept.confidence_score > 0.0
    assert "Finance" in (resp_dept.structured_data.get("relieving_letter_draft") or "")


# 14. Final Settlement Unused Leave and Unpaid Work Days Required
def test_final_settlement_unused_leave_and_unpaid_work_days_required(supervisor):
    base_ctx = {
        "employee_id": "EMP-01",
        "employee_name": "Tariq",
        "basic_wage_monthly": 12000.0,
        "gross_wage_monthly": 20000.0,
        "tenure_years": 3.0,
    }
    # Missing both
    resp_both = supervisor.process_message(
        message="Calculate final settlement fnf for employee resignation",
        jurisdiction=Jurisdiction.UAE,
        auth=ADMIN,
        context=base_ctx,
    )
    assert resp_both.confidence_score == 0.0
    assert resp_both.structured_data == {
        "error": "MISSING_INPUT",
        "missing": ["unused_leave_days", "unpaid_work_days"],
    }

    # Missing unused_leave_days only
    ctx_no_leave = dict(base_ctx, unpaid_work_days=0)
    resp_no_leave = supervisor.process_message(
        message="Calculate final settlement fnf for employee resignation",
        jurisdiction=Jurisdiction.UAE,
        auth=ADMIN,
        context=ctx_no_leave,
    )
    assert resp_no_leave.confidence_score == 0.0
    assert resp_no_leave.structured_data == {"error": "MISSING_INPUT", "missing": ["unused_leave_days"]}

    # Missing unpaid_work_days only
    ctx_no_unpaid = dict(base_ctx, unused_leave_days=5.0)
    resp_no_unpaid = supervisor.process_message(
        message="Calculate final settlement fnf for employee resignation",
        jurisdiction=Jurisdiction.UAE,
        auth=ADMIN,
        context=ctx_no_unpaid,
    )
    assert resp_no_unpaid.confidence_score == 0.0
    assert resp_no_unpaid.structured_data == {"error": "MISSING_INPUT", "missing": ["unpaid_work_days"]}

    # Boundary: 0 and 0.0 are valid supplied values
    ctx_zeros = dict(base_ctx, unused_leave_days=0.0, unpaid_work_days=0)
    resp_zeros = supervisor.process_message(
        message="Calculate final settlement fnf for employee resignation",
        jurisdiction=Jurisdiction.UAE,
        auth=ADMIN,
        context=ctx_zeros,
    )
    assert resp_zeros.confidence_score > 0.0
    assert "total_net_payable" in resp_zeros.structured_data


# 15. Onboarding Provisioning Department Optional Not Invented
def test_onboarding_provisioning_department_optional_not_invented(supervisor):
    # Department omitted -> structured_data["department"] is None, never "Engineering"
    resp_no_dept = supervisor.process_message(
        message="Generate provision plan for new hire",
        jurisdiction=Jurisdiction.INDIA,
        auth=ADMIN,
        context={"candidate_name": "Rashid", "employee_id": "EMP-900", "role": "DevOps Engineer"},
    )
    assert resp_no_dept.confidence_score > 0.0
    assert resp_no_dept.structured_data.get("department") is None
    assert "Engineering" not in resp_no_dept.reply_text

    # Department supplied -> preserved
    resp_dept = supervisor.process_message(
        message="Generate provision plan for new hire",
        jurisdiction=Jurisdiction.INDIA,
        auth=ADMIN,
        context={
            "candidate_name": "Rashid",
            "employee_id": "EMP-900",
            "role": "DevOps Engineer",
            "department": "Infrastructure",
        },
    )
    assert resp_dept.confidence_score > 0.0
    assert resp_dept.structured_data.get("department") == "Infrastructure"


# 16. Travel Expense Tier and Pre-auth Required
def test_travel_expense_tier_and_preauth_required(supervisor):
    base_ctx = {
        "claim_id": "CLM-55",
        "employee_id": "EMP-TRV",
        "expenses": [{"id": "EXP-1", "category": "MEAL", "amount": "1500.00", "has_receipt": True}],
    }
    # Missing both tier and has_approved_travel_request
    resp_both = supervisor.process_message(
        message="Audit travel expense claim and reimbursement claim",
        jurisdiction=Jurisdiction.INDIA,
        auth=ADMIN,
        context=base_ctx,
    )
    assert resp_both.confidence_score == 0.0
    assert resp_both.structured_data == {
        "error": "MISSING_INPUT",
        "missing": ["tier", "has_approved_travel_request"],
    }

    # Missing tier only
    resp_no_tier = supervisor.process_message(
        message="Audit travel expense claim and reimbursement claim",
        jurisdiction=Jurisdiction.INDIA,
        auth=ADMIN,
        context=dict(base_ctx, has_approved_travel_request=True),
    )
    assert resp_no_tier.confidence_score == 0.0
    assert resp_no_tier.structured_data == {"error": "MISSING_INPUT", "missing": ["tier"]}

    # Missing has_approved_travel_request only
    resp_no_preauth = supervisor.process_message(
        message="Audit travel expense claim and reimbursement claim",
        jurisdiction=Jurisdiction.INDIA,
        auth=ADMIN,
        context=dict(base_ctx, tier="METRO"),
    )
    assert resp_no_preauth.confidence_score == 0.0
    assert resp_no_preauth.structured_data == {"error": "MISSING_INPUT", "missing": ["has_approved_travel_request"]}

    # Boundary: has_approved_travel_request=False is valid (disallows flights/hotels)
    resp_unapproved = supervisor.process_message(
        message="Audit travel expense claim and reimbursement claim",
        jurisdiction=Jurisdiction.INDIA,
        auth=ADMIN,
        context={
            "claim_id": "CLM-55",
            "employee_id": "EMP-TRV",
            "expenses": [{"id": "EXP-1", "category": "FLIGHT", "amount": "5000.00", "has_receipt": True}],
            "tier": "METRO",
            "has_approved_travel_request": False,
        },
    )
    assert resp_unapproved.confidence_score > 0.0
    assert resp_unapproved.structured_data["is_fully_approved"] is False


# 17. HTTP non-agentic endpoint checks for compliance and clearance missing inputs
def test_http_chat_compliance_and_clearance_missing_input(make_client, bearer):
    client = make_client()
    headers = bearer(roles=["hr_manager"])

    # Compliance audit missing employees
    res_comp = client.post(
        "/v1/chat",
        headers=headers,
        json={"message": "Run statutory compliance audit for employees", "jurisdiction": "IN", "agentic": False,
              "context": {}},
    )
    assert res_comp.status_code == 200
    data_comp = res_comp.json()
    assert data_comp["confidence_score"] == 0.0
    assert data_comp["structured_data"] == {"error": "MISSING_INPUT", "missing": ["employees"]}

    # Exit clearance missing inputs
    res_clr = client.post(
        "/v1/chat",
        headers=headers,
        json={"message": "Process exit clearance for offboard employee", "jurisdiction": "AE", "agentic": False,
              "context": {}},
    )
    assert res_clr.status_code == 200
    data_clr = res_clr.json()
    assert data_clr["confidence_score"] == 0.0
    assert data_clr["structured_data"] == {
        "error": "MISSING_INPUT",
        "missing": ["employee_id", "employee_name", "returned_assets", "pending_assets", "finance_cleared"],
    }
    assert _has_no_figures(data_clr["reply_text"])
    assert "Engineering" not in data_clr["reply_text"]
