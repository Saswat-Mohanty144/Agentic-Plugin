"""Cognitive Multi-Agent Supervisor: Intent Router & Orchestration Brain."""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from pydantic import BaseModel, Field

from hrms_plugin.agents.compliance import ComplianceAgent
from hrms_plugin.agents.leave_attendance import LeaveAttendanceAgent
from hrms_plugin.agents.offboarding import OffboardingAgent
from hrms_plugin.agents.onboarding import OnboardingAgent
from hrms_plugin.agents.recruitment import RecruitmentAgent
from hrms_plugin.agents.statutory_payroll import StatutoryPayrollAgent
from hrms_plugin.agents.travel_expense import TravelExpenseAgent
from hrms_plugin.rag.store import Jurisdiction, StatutoryKnowledgeBase
from hrms_plugin.security.rbac import AuthContext, Permission, PermissionDenied, RbacPolicyEnforcer


class UserIntent(str, Enum):
    PAYROLL_STRUCTURE = "PAYROLL_STRUCTURE"
    PAYROLL_EOSB = "PAYROLL_EOSB"
    COMPLIANCE_AUDIT = "COMPLIANCE_AUDIT"
    LEAVE_APPLY = "LEAVE_APPLY"
    ATTENDANCE_ANOMALY = "ATTENDANCE_ANOMALY"
    RECRUITMENT_ATS = "RECRUITMENT_ATS"
    RECRUITMENT_BIAS = "RECRUITMENT_BIAS"
    RECRUITMENT_REQUISITION = "RECRUITMENT_REQUISITION"
    ONBOARDING = "ONBOARDING"
    OFFBOARDING_FNF = "OFFBOARDING_FNF"
    TRAVEL_EXPENSE = "TRAVEL_EXPENSE"
    LEGAL_QNA = "LEGAL_QNA"
    GENERAL_HR = "GENERAL_HR"


ONBOARDING_DOCUMENT_KEYWORDS = ("document", "kyc", "pan", "aadhaar", "emirates id")

INTENT_PERMISSIONS: Dict[UserIntent, Permission] = {
    UserIntent.PAYROLL_STRUCTURE: Permission.VIEW_SALARY,
    UserIntent.PAYROLL_EOSB: Permission.VIEW_SALARY,
    UserIntent.COMPLIANCE_AUDIT: Permission.VIEW_COMPLIANCE,
    UserIntent.LEAVE_APPLY: Permission.APPLY_LEAVE,
    UserIntent.ATTENDANCE_ANOMALY: Permission.APPROVE_LEAVE,
    UserIntent.RECRUITMENT_ATS: Permission.MANAGE_RECRUITMENT,
    UserIntent.RECRUITMENT_BIAS: Permission.MANAGE_RECRUITMENT,
    UserIntent.RECRUITMENT_REQUISITION: Permission.MANAGE_RECRUITMENT,
    UserIntent.OFFBOARDING_FNF: Permission.VIEW_SALARY,
    UserIntent.TRAVEL_EXPENSE: Permission.APPROVE_EXPENSE,
    UserIntent.LEGAL_QNA: Permission.USE_COPILOT,
    UserIntent.GENERAL_HR: Permission.USE_COPILOT,
}


def required_permission(intent: UserIntent, message_lower: str) -> Permission:
    """Permission for the action process_message will take for this intent and message."""
    if intent == UserIntent.ONBOARDING:
        if any(k in message_lower for k in ONBOARDING_DOCUMENT_KEYWORDS):
            return Permission.VIEW_EMPLOYEE
        return Permission.MANAGE_RECRUITMENT
    return INTENT_PERMISSIONS[intent]


# Ordered: the first match wins in classify_intent; matching_intents returns every match (tool subsets, QE-2).
INTENT_KEYWORDS: Tuple[Tuple[UserIntent, float, Tuple[str, ...]], ...] = (
    (UserIntent.PAYROLL_EOSB, 0.95, ("eosb", "end of service", "gratuity calculation", "severance")),
    (UserIntent.PAYROLL_STRUCTURE, 0.92, ("salary structure", "ctc breakdown", "calculate salary", "take home",
                                          "gross to net", "pf deduction")),
    (UserIntent.COMPLIANCE_AUDIT, 0.90, ("compliance", "labor law", "wps audit", "violation", "statutory audit",
                                         "probation limit")),
    (UserIntent.LEAVE_APPLY, 0.92, ("apply leave", "leave balance", "vacation request", "sick leave",
                                    "casual leave")),
    (UserIntent.ATTENDANCE_ANOMALY, 0.94, ("impossible travel", "punch anomaly", "check-in location", "ghost punch",
                                           "attendance fraud")),
    (UserIntent.RECRUITMENT_ATS, 0.91, ("score resume", "ats score", "match candidate", "screen applicant")),
    (UserIntent.RECRUITMENT_BIAS, 0.93, ("bias", "inclusive jd", "gender neutral", "audit job description")),
    (UserIntent.ONBOARDING, 0.92, ("onboard", "offer letter", "pre-hire", "verify kyc", "pan verification",
                                   "aadhaar check", "emirates id", "it provisioning", "new hire")),
    (UserIntent.OFFBOARDING_FNF, 0.93, ("offboard", "fnf", "final settlement", "settlement", "resignation",
                                        "notice period buyout", "relieving letter", "exit clearance",
                                        "last working day", "article 53")),
    (UserIntent.TRAVEL_EXPENSE, 0.92, ("travel expense", "reimbursement claim", "per diem", "per-diem",
                                       "expense audit", "hotel claim", "flight expense", "meal allowance")),
    (UserIntent.LEGAL_QNA, 0.88, ("labor code", "section", "article", "gazette", "overtime rule", "maternity law")),
)


# Everyday whole words that only widen tool-subset selection (token waste #3); classify_intent ignores them so the
# non-agentic first-match routing is unchanged.
SUBSET_WORDS: Tuple[Tuple[UserIntent, Tuple[str, ...]], ...] = (
    (UserIntent.PAYROLL_EOSB, ("gratuity", "eosb")),
    (UserIntent.PAYROLL_STRUCTURE, ("salary", "ctc", "payslip", "pf", "epf", "payroll")),
    (UserIntent.COMPLIANCE_AUDIT, ("audit", "wps")),
    (UserIntent.LEAVE_APPLY, ("leave", "vacation")),
    (UserIntent.ATTENDANCE_ANOMALY, ("punch", "attendance")),
    (UserIntent.RECRUITMENT_ATS, ("resume", "cv", "candidate")),
    (UserIntent.ONBOARDING, ("onboarding", "joining")),
    (UserIntent.OFFBOARDING_FNF, ("resign", "resigned", "exit")),
    (UserIntent.TRAVEL_EXPENSE, ("expense", "expenses", "reimbursement")),
    (UserIntent.RECRUITMENT_REQUISITION, ("requisition", "requisitions", "hiring")),
)
_SUBSET_PATTERNS = tuple((intent, re.compile(r"\b(" + "|".join(words) + r")\b")) for intent, words in SUBSET_WORDS)


def matching_intents(message: str) -> Set[UserIntent]:
    msg = (message or "").lower()
    found = {intent for intent, _, keywords in INTENT_KEYWORDS if any(w in msg for w in keywords)}
    return found | {intent for intent, pattern in _SUBSET_PATTERNS if pattern.search(msg)}


class AgentResponse(BaseModel):
    intent: UserIntent
    routed_agent: str
    reply_text: str
    confidence_score: float
    structured_data: Optional[Dict[str, Any]] = None
    statutory_citations: List[str] = Field(default_factory=list)
    suggested_actions: List[Dict[str, Any]] = Field(default_factory=list)
    thought_process: Optional[List[str]] = Field(default_factory=list)
    tool_calls: Optional[List[Dict[str, Any]]] = Field(default_factory=list)
    truncated: bool = False  # the final answer was stopped by its output cap (#9)
    partial: bool = False  # the step limit was reached; answer built from the results gathered so far (#5)
    history_trimmed: int = 0  # oldest history turns dropped to fit the budget (token waste #7)


_MISSING_INPUT_FIELD_LABELS: Dict[str, str] = {
    "annual_ctc": "annual CTC",
    "basic_wage_monthly": "monthly basic wage",
    "tenure_years": "tenure in years",
    "employee_id": "employee ID",
    "employee_name": "employee name",
    "requested_days": "requested days",
    "current_balance": "current balance",
    "punch1": "initial attendance punch",
    "punch2": "second attendance punch",
    "resume_text": "resume text",
    "required_skills": "required skills",
    "required_experience_years": "required experience years",
    "candidate_experience_years": "candidate experience years",
    "candidate_name": "candidate name",
    "documents": "submitted documents",
    "role": "role",
    "job_title": "job title",
    "annual_salary": "annual salary",
    "last_working_day": "last working day",
    "gross_wage_monthly": "monthly gross wage",
    "claim_id": "claim ID",
    "expenses": "expense items",
    "employees": "employee records",
    "leave_type": "leave type",
    "returned_assets": "returned assets",
    "pending_assets": "pending assets",
    "finance_cleared": "finance clearance status",
    "unused_leave_days": "unused leave days",
    "unpaid_work_days": "unpaid work days",
    "tier": "travel tier",
    "has_approved_travel_request": "approved travel request status",
}


def _missing_input_response(
    intent: UserIntent,
    routed_agent: str,
    missing: List[str],
) -> AgentResponse:
    labels = [_MISSING_INPUT_FIELD_LABELS.get(k, k.replace("_", " ")) for k in missing]
    return AgentResponse(
        intent=intent,
        routed_agent=routed_agent,
        reply_text=f"Please provide the missing inputs ({', '.join(labels)}) to proceed.",
        confidence_score=0.0,
        structured_data={"error": "MISSING_INPUT", "missing": missing},
    )


class SupervisorAgent:
    """Central supervisor coordinating domain specialists and statutory legal RAG."""

    def __init__(
        self,
        kb: Optional[StatutoryKnowledgeBase] = None,
        payroll_agent: Optional[StatutoryPayrollAgent] = None,
        compliance_agent: Optional[ComplianceAgent] = None,
        leave_agent: Optional[LeaveAttendanceAgent] = None,
        recruitment_agent: Optional[RecruitmentAgent] = None,
        onboarding_agent: Optional[OnboardingAgent] = None,
        offboarding_agent: Optional[OffboardingAgent] = None,
        travel_agent: Optional[TravelExpenseAgent] = None,
        llm_gateway: Optional[Any] = None,
        tool_registry: Optional[Any] = None,
        *,
        classification_for: Optional[Callable[[str, str], Optional[Dict[str, Any]]]] = None,
    ):
        self.kb = kb or StatutoryKnowledgeBase()
        self.payroll_agent = payroll_agent or StatutoryPayrollAgent(kb=self.kb)
        self.compliance_agent = compliance_agent or ComplianceAgent(kb=self.kb)
        self.leave_agent = leave_agent or LeaveAttendanceAgent()
        self.recruitment_agent = recruitment_agent or RecruitmentAgent()
        self.onboarding_agent = onboarding_agent or OnboardingAgent()
        self.offboarding_agent = offboarding_agent or OffboardingAgent(kb=self.kb, payroll_agent=self.payroll_agent)
        self.travel_agent = travel_agent or TravelExpenseAgent()
        self.llm_gateway = llm_gateway
        self.tool_registry = tool_registry
        self.classification_for = classification_for
        self._orchestrator = None
        self._rbac = RbacPolicyEnforcer()

    def classify_intent(self, message: str) -> tuple[UserIntent, float]:
        """Classify user intent using deterministic keyword intent heuristic with confidence scoring."""
        msg = message.lower()
        for intent, confidence, keywords in INTENT_KEYWORDS:
            if any(w in msg for w in keywords):
                return intent, confidence
        return UserIntent.GENERAL_HR, 0.70

    def process_message(
        self,
        message: str,
        jurisdiction: Jurisdiction | str = Jurisdiction.INDIA,
        context: Optional[Dict[str, Any]] = None,
        *,
        auth: AuthContext,
    ) -> AgentResponse:
        """Route message to appropriate specialist agent and generate unified response."""
        intent, confidence = self.classify_intent(message)
        jur = Jurisdiction(jurisdiction) if isinstance(jurisdiction, str) else jurisdiction
        ctx = context or {}
        msg = message.lower()
        permission = required_permission(intent, msg)
        if not self._rbac.is_authorized(auth, permission):
            raise PermissionDenied(permission)

        # 1. Statutory Salary Structuring
        if intent == UserIntent.PAYROLL_STRUCTURE:
            ctc_val = ctx.get("annual_ctc") if ctx.get("annual_ctc") is not None else ctx.get("ctc")
            if ctc_val is None:
                return _missing_input_response(intent, "StatutoryPayrollAgent", ["annual_ctc"])
            ctc = float(ctc_val)
            if jur == Jurisdiction.UAE:
                res_uae = self.payroll_agent.structure_uae_salary(monthly_gross=ctc / 12.0)
                reply = (
                    f"Structured UAE Monthly Salary of AED {res_uae.monthly_gross:,.2f} with "
                    f"Basic AED {res_uae.basic_wage:,.2f} and Housing AED {res_uae.housing_allowance:,.2f}."
                )
                return AgentResponse(
                    intent=intent,
                    routed_agent="StatutoryPayrollAgent",
                    reply_text=reply,
                    confidence_score=confidence,
                    structured_data=res_uae.model_dump(),
                    statutory_citations=res_uae.statutory_citations,
                )
            else:
                res_in = self.payroll_agent.structure_india_salary(annual_ctc=ctc)
                reply = (
                    f"Computed India CTC of INR {res_in.annual_ctc:,.2f}. "
                    f"Monthly Gross: INR {res_in.gross_salary:,.2f}, Net Take-Home: INR {res_in.net_take_home:,.2f}."
                )
                return AgentResponse(
                    intent=intent,
                    routed_agent="StatutoryPayrollAgent",
                    reply_text=reply,
                    confidence_score=confidence,
                    structured_data=res_in.model_dump(),
                    statutory_citations=res_in.statutory_citations,
                )

        # 2. UAE EOSB Calculation
        elif intent == UserIntent.PAYROLL_EOSB:
            missing = []
            basic_val = (
                ctx.get("basic_wage_monthly") if ctx.get("basic_wage_monthly") is not None else ctx.get("basic")
            )
            if basic_val is None:
                missing.append("basic_wage_monthly")
            tenure_val = ctx.get("tenure_years") if ctx.get("tenure_years") is not None else ctx.get("tenure")
            if tenure_val is None:
                missing.append("tenure_years")
            if missing:
                return _missing_input_response(intent, "StatutoryPayrollAgent", missing)
            basic = float(basic_val)
            tenure = float(tenure_val)
            eosb_res = self.payroll_agent.calculate_uae_eosb(basic_wage_monthly=basic, tenure_years=tenure)
            reply = (
                f"Calculated UAE End of Service Gratuity: AED {eosb_res.total_eosb_gratuity:,.2f} "
                f"for {eosb_res.tenure_years} years of service."
            )
            return AgentResponse(
                intent=intent,
                routed_agent="StatutoryPayrollAgent",
                reply_text=reply,
                confidence_score=confidence,
                structured_data=eosb_res.model_dump(),
                statutory_citations=[eosb_res.statutory_citation],
            )

        # 3. Compliance Audit
        elif intent == UserIntent.COMPLIANCE_AUDIT:
            tenant_id = auth.tenant_id  # never from request context (agy F1)
            employees = ctx.get("employees")
            if not employees:
                return _missing_input_response(intent, "ComplianceAgent", ["employees"])
            audit_res = self.compliance_agent.audit_employees(
                tenant_id=tenant_id, jurisdiction=jur, employees=employees
            )
            status_text = (
                "COMPLIANT (0 violations)"
                if audit_res.is_compliant
                else f"NON-COMPLIANT ({audit_res.violations_count} violations detected)"
            )
            return AgentResponse(
                intent=intent,
                routed_agent="ComplianceAgent",
                reply_text=f"Compliance Audit completed for {tenant_id} [{jur.value}]. Result: {status_text}.",
                confidence_score=confidence,
                structured_data=audit_res.model_dump(),
                statutory_citations=[v.statutory_citation for v in audit_res.violations],
                suggested_actions=[
                    {
                        "action": "REMEDIATE_PATCH",
                        "violation_id": v.violation_id,
                        "entity_id": v.entity_id,
                        "entity_type": v.entity_type,
                        "patch": v.remediation_patch,
                    }
                    for v in audit_res.violations
                    if v.remediation_patch
                ],
            )

        # 4. Leave Application
        elif intent == UserIntent.LEAVE_APPLY:
            missing = []
            if ctx.get("employee_id") is None:
                missing.append("employee_id")
            if ctx.get("leave_type") is None:
                missing.append("leave_type")
            if ctx.get("requested_days") is None:
                missing.append("requested_days")
            if ctx.get("current_balance") is None:
                missing.append("current_balance")
            if missing:
                return _missing_input_response(intent, "LeaveAttendanceAgent", missing)
            emp_id = str(ctx["employee_id"])
            leave_type = str(ctx["leave_type"])
            req_days = float(ctx["requested_days"])
            cur_bal = float(ctx["current_balance"])
            leave_res = self.leave_agent.evaluate_leave_request(
                employee_id=emp_id,
                leave_type=leave_type,
                requested_days=req_days,
                current_balance=cur_bal,
            )
            decision = "Approved" if leave_res.is_approved else f"Rejected ({leave_res.rejection_reason})"
            reply = (
                f"Leave application of {req_days} days {leave_type} for {emp_id}: "
                f"{decision}. Remaining balance: {leave_res.remaining_balance} days."
            )
            return AgentResponse(
                intent=intent,
                routed_agent="LeaveAttendanceAgent",
                reply_text=reply,
                confidence_score=confidence,
                structured_data=leave_res.model_dump(),
            )

        # 5. Impossible Travel
        elif intent == UserIntent.ATTENDANCE_ANOMALY:
            missing = []
            if ctx.get("punch1") is None:
                missing.append("punch1")
            if ctx.get("punch2") is None:
                missing.append("punch2")
            if missing:
                return _missing_input_response(intent, "LeaveAttendanceAgent", missing)
            p1 = ctx["punch1"]
            p2 = ctx["punch2"]
            anomaly_res = self.leave_agent.detect_impossible_travel(p1, p2)
            return AgentResponse(
                intent=intent,
                routed_agent="LeaveAttendanceAgent",
                reply_text=anomaly_res.explanation,
                confidence_score=confidence,
                structured_data=anomaly_res.model_dump(),
            )

        # 6. Recruitment ATS Scoring
        elif intent == UserIntent.RECRUITMENT_ATS:
            missing = []
            if ctx.get("resume_text") is None:
                missing.append("resume_text")
            if ctx.get("required_skills") is None:
                missing.append("required_skills")
            if ctx.get("required_experience_years") is None:
                missing.append("required_experience_years")
            if ctx.get("candidate_experience_years") is None:
                missing.append("candidate_experience_years")
            if missing:
                return _missing_input_response(intent, "RecruitmentAgent", missing)
            resume = str(ctx["resume_text"])
            skills = ctx["required_skills"]
            req_exp = float(ctx["required_experience_years"])
            cand_exp = float(ctx["candidate_experience_years"])
            ats_res = self.recruitment_agent.score_candidate(
                resume_text=resume,
                required_skills=skills,
                required_experience_years=req_exp,
                candidate_experience_years=cand_exp,
            )
            reply = (
                f"ATS Candidate Score: {ats_res.composite_score}/100. "
                f"Recommendation: {ats_res.recommendation}. {ats_res.explanation}"
            )
            return AgentResponse(
                intent=intent,
                routed_agent="RecruitmentAgent",
                reply_text=reply,
                confidence_score=confidence,
                structured_data=ats_res.model_dump(),
            )

        # 7. Recruitment Bias Audit
        elif intent == UserIntent.RECRUITMENT_BIAS:
            jd_text = str(ctx.get("jd_text", message))
            bias_res = self.recruitment_agent.audit_job_description_bias(jd_text)
            reply = (
                "No exclusionary or biased terms detected."
                if not bias_res.has_bias
                else f"Detected {len(bias_res.detected_terms)} biased term(s). Suggested inclusive rewrites applied."
            )
            return AgentResponse(
                intent=intent,
                routed_agent="RecruitmentAgent",
                reply_text=reply,
                confidence_score=confidence,
                structured_data=bias_res.model_dump(),
            )

        # 8. Onboarding Specialist
        elif intent == UserIntent.ONBOARDING:
            if any(k in msg for k in ONBOARDING_DOCUMENT_KEYWORDS):
                missing = []
                if ctx.get("candidate_name") is None:
                    missing.append("candidate_name")
                docs_val = (
                    ctx.get("documents") if ctx.get("documents") is not None else ctx.get("submitted_documents")
                )
                if docs_val is None:
                    missing.append("documents")
                if missing:
                    return _missing_input_response(intent, "OnboardingAgent", missing)
                cand_name = str(ctx["candidate_name"])
                docs = docs_val
                doc_res = self.onboarding_agent.verify_onboarding_documents(
                    candidate_name=cand_name,
                    jurisdiction=jur,
                    submitted_documents=docs,
                )
                reply = (
                    f"KYC Verification for {cand_name} [{jur.value}]: {doc_res.status.value}. "
                    f"Verified: {len(doc_res.verified_documents)}, Missing: {len(doc_res.missing_documents)}."
                )
                return AgentResponse(
                    intent=intent,
                    routed_agent="OnboardingAgent",
                    reply_text=reply,
                    confidence_score=confidence,
                    structured_data=doc_res.model_dump(),
                    statutory_citations=doc_res.statutory_citations,
                )
            elif "provision" in msg:
                missing = []
                if ctx.get("candidate_name") is None:
                    missing.append("candidate_name")
                if ctx.get("employee_id") is None:
                    missing.append("employee_id")
                if ctx.get("role") is None:
                    missing.append("role")
                if missing:
                    return _missing_input_response(intent, "OnboardingAgent", missing)
                cand_name = str(ctx["candidate_name"])
                emp_id = str(ctx["employee_id"])
                dept = str(ctx["department"]) if ctx.get("department") is not None else None
                role = str(ctx["role"])
                buddy = ctx.get("buddy_name")
                prov_res = self.onboarding_agent.generate_provisioning_plan(
                    employee_id=emp_id,
                    employee_name=cand_name,
                    department=dept,
                    role=role,
                    buddy_name=buddy,
                )
                reply = (
                    f"Provisioning plan generated for {cand_name} ({emp_id}). "
                    f"{len(prov_res.it_assets)} IT assets and {len(prov_res.system_accounts)} accounts scheduled."
                )
                return AgentResponse(
                    intent=intent,
                    routed_agent="OnboardingAgent",
                    reply_text=reply,
                    confidence_score=confidence,
                    structured_data=prov_res.model_dump(),
                )
            else:
                missing = []
                if ctx.get("candidate_name") is None:
                    missing.append("candidate_name")
                if ctx.get("job_title") is None:
                    missing.append("job_title")
                salary_val = ctx.get("annual_salary") if ctx.get("annual_salary") is not None else ctx.get("salary")
                if salary_val is None:
                    missing.append("annual_salary")
                currency_val = ctx.get("currency")
                if currency_val is None:
                    if jur in (Jurisdiction.INDIA, "IN"):
                        currency_val = "INR"
                    elif jur in (Jurisdiction.UAE, "AE"):
                        currency_val = "AED"
                    else:
                        missing.append("currency")
                if missing:
                    return _missing_input_response(intent, "OnboardingAgent", missing)
                cand_name = str(ctx["candidate_name"])
                title = str(ctx["job_title"])
                salary = float(salary_val)
                currency = str(currency_val)
                b_min = ctx.get("band_min")
                b_max = ctx.get("band_max")
                offer_res = self.onboarding_agent.evaluate_offer(
                    candidate_name=cand_name,
                    job_title=title,
                    annual_salary=salary,
                    currency=currency,
                    band_min=b_min,
                    band_max=b_max,
                    jurisdiction=jur,
                )
                reply = f"Offer evaluation for {cand_name} ({title}): {offer_res.status.value}. {offer_res.explanation}"
                citations = [offer_res.statutory_citation] if offer_res.statutory_citation else []
                return AgentResponse(
                    intent=intent,
                    routed_agent="OnboardingAgent",
                    reply_text=reply,
                    confidence_score=confidence,
                    structured_data=offer_res.model_dump(mode="json"),
                    statutory_citations=citations,
                )

        # 9. Offboarding & Final Settlement (FNF)
        elif intent == UserIntent.OFFBOARDING_FNF:
            if any(k in msg for k in ["timeline", "article 53", "deadline", "14-day"]):
                missing = []
                if ctx.get("employee_id") is None:
                    missing.append("employee_id")
                if ctx.get("employee_name") is None:
                    missing.append("employee_name")
                if ctx.get("last_working_day") is None:
                    missing.append("last_working_day")
                if missing:
                    return _missing_input_response(intent, "OffboardingAgent", missing)
                emp_id = str(ctx["employee_id"])
                emp_name = str(ctx["employee_name"])
                lwd = str(ctx["last_working_day"])
                today_str = ctx.get("current_date")
                audit_res = self.offboarding_agent.audit_uae_settlement_timeline(
                    employee_id=emp_id,
                    employee_name=emp_name,
                    last_working_day=lwd,
                    current_date_str=today_str,
                )
                reply = (
                    f"UAE Article 53 settlement audit for {emp_name}: {audit_res.status.value}. "
                    f"{audit_res.days_elapsed} days elapsed, {audit_res.days_remaining} days remaining."
                )
                return AgentResponse(
                    intent=intent,
                    routed_agent="OffboardingAgent",
                    reply_text=reply,
                    confidence_score=confidence,
                    structured_data=audit_res.model_dump(),
                    statutory_citations=[audit_res.statutory_citation],
                )
            elif "clearance" in msg:
                missing = []
                if ctx.get("employee_id") is None:
                    missing.append("employee_id")
                if ctx.get("employee_name") is None:
                    missing.append("employee_name")
                if ctx.get("returned_assets") is None:
                    missing.append("returned_assets")
                if ctx.get("pending_assets") is None:
                    missing.append("pending_assets")
                if ctx.get("finance_cleared") is None:
                    missing.append("finance_cleared")
                if missing:
                    return _missing_input_response(intent, "OffboardingAgent", missing)
                emp_id = str(ctx["employee_id"])
                emp_name = str(ctx["employee_name"])
                dept = str(ctx["department"]) if ctx.get("department") is not None else None
                returned = list(ctx["returned_assets"])
                pending = list(ctx["pending_assets"])
                fin_cleared = bool(ctx["finance_cleared"])
                clr_res = self.offboarding_agent.evaluate_exit_clearance(
                    employee_id=emp_id,
                    employee_name=emp_name,
                    department=dept,
                    returned_assets=returned,
                    pending_assets=pending,
                    finance_cleared=fin_cleared,
                )
                status_str = "CLEARED" if clr_res.is_fully_cleared else "PENDING ITEMS"
                reply = f"Exit clearance for {emp_name}: {status_str}."
                return AgentResponse(
                    intent=intent,
                    routed_agent="OffboardingAgent",
                    reply_text=reply,
                    confidence_score=confidence,
                    structured_data=clr_res.model_dump(),
                )
            else:
                missing = []
                if ctx.get("employee_id") is None:
                    missing.append("employee_id")
                if ctx.get("employee_name") is None:
                    missing.append("employee_name")
                basic_val = (
                    ctx.get("basic_wage_monthly") if ctx.get("basic_wage_monthly") is not None else ctx.get("basic")
                )
                if basic_val is None:
                    missing.append("basic_wage_monthly")
                gross_val = (
                    ctx.get("gross_wage_monthly") if ctx.get("gross_wage_monthly") is not None else ctx.get("gross")
                )
                if gross_val is None:
                    missing.append("gross_wage_monthly")
                tenure_val = ctx.get("tenure_years") if ctx.get("tenure_years") is not None else ctx.get("tenure")
                if tenure_val is None:
                    missing.append("tenure_years")
                if ctx.get("unused_leave_days") is None:
                    missing.append("unused_leave_days")
                if ctx.get("unpaid_work_days") is None:
                    missing.append("unpaid_work_days")
                if missing:
                    return _missing_input_response(intent, "OffboardingAgent", missing)
                emp_id = str(ctx["employee_id"])
                emp_name = str(ctx["employee_name"])
                basic = float(basic_val)
                gross = float(gross_val)
                tenure = float(tenure_val)
                leave_days = float(ctx["unused_leave_days"])
                unpaid_days = int(ctx["unpaid_work_days"])
                fnf_res = self.offboarding_agent.compute_final_settlement(
                    employee_id=emp_id,
                    employee_name=emp_name,
                    basic_wage_monthly=basic,
                    gross_wage_monthly=gross,
                    tenure_years=tenure,
                    unused_leave_days=leave_days,
                    unpaid_work_days=unpaid_days,
                    jurisdiction=jur,
                )
                cur_sym = "AED" if jur == Jurisdiction.UAE else "INR"
                reply = (
                    f"Final settlement for {emp_name} ({emp_id}): Total Net Payable {cur_sym} "
                    f"{fnf_res.total_net_payable:,.2f}."
                )
                return AgentResponse(
                    intent=intent,
                    routed_agent="OffboardingAgent",
                    reply_text=reply,
                    confidence_score=confidence,
                    structured_data=fnf_res.model_dump(mode="json"),
                    statutory_citations=fnf_res.statutory_citations,
                )

        # 10. Travel & Expense Claim Audit
        elif intent == UserIntent.TRAVEL_EXPENSE:
            missing = []
            if ctx.get("claim_id") is None:
                missing.append("claim_id")
            if ctx.get("employee_id") is None:
                missing.append("employee_id")
            if ctx.get("expenses") is None:
                missing.append("expenses")
            if ctx.get("tier") is None:
                missing.append("tier")
            if ctx.get("has_approved_travel_request") is None:
                missing.append("has_approved_travel_request")
            cur_code = ctx.get("currency")
            if cur_code is None:
                if jur in (Jurisdiction.INDIA, "IN"):
                    cur_code = "INR"
                elif jur in (Jurisdiction.UAE, "AE"):
                    cur_code = "AED"
                else:
                    missing.append("currency")
            if missing:
                return _missing_input_response(intent, "TravelExpenseAgent", missing)
            claim_id = str(ctx["claim_id"])
            emp_id = str(ctx["employee_id"])
            expenses = ctx["expenses"]
            cur_code = str(cur_code)
            tier = str(ctx["tier"])
            has_pre_auth = bool(ctx["has_approved_travel_request"])
            exp_res = self.travel_agent.audit_claim(
                claim_id=claim_id,
                employee_id=emp_id,
                expenses=expenses,
                currency=cur_code,
                tier=tier,
                has_approved_travel_request=has_pre_auth,
            )
            reply = (
                f"Expense Claim {claim_id} Audit: Total Claimed {cur_code} {exp_res.total_claimed:,.2f}, "
                f"Approved: {cur_code} {exp_res.total_approved:,.2f}, Disallowed: {cur_code} "
                f"{exp_res.total_disallowed:,.2f}."
            )
            return AgentResponse(
                intent=intent,
                routed_agent="TravelExpenseAgent",
                reply_text=reply,
                confidence_score=confidence,
                structured_data=exp_res.model_dump(mode="json"),
            )

        # 11. Legal Knowledge RAG
        elif intent == UserIntent.LEGAL_QNA:
            citations = self.kb.search(query=message, jurisdiction=jur, limit=3)
            formatted = [self.kb.format_citation(c) for c in citations]
            summary_text = "\n".join([f"• {c.title}: {c.summary}" for c in citations])
            return AgentResponse(
                intent=intent,
                routed_agent="StatutoryKnowledgeBase",
                reply_text=f"Found {len(citations)} statutory reference(s) for [{jur.value}]:\n{summary_text}",
                confidence_score=confidence,
                statutory_citations=formatted,
                structured_data={"results_count": len(citations)},
            )

        # 12. General HR Fallback
        return AgentResponse(
            intent=UserIntent.GENERAL_HR,
            routed_agent="SupervisorAgent",
            reply_text=(
                f"Received HR inquiry: '{message}'. How can I assist with Payroll, "
                "Compliance, Leave, Attendance, Recruitment, Onboarding, Offboarding, or Travel Claims?"
            ),
            confidence_score=confidence,
        )

    def get_orchestrator(self):
        """Lazy-initialize AgenticOrchestrator to avoid circular imports."""
        if self._orchestrator is None:
            from hrms_plugin.agents.orchestrator import AgenticOrchestrator
            from hrms_plugin.agents.tools import AgenticToolRegistry
            from hrms_plugin.ai.llm import LLMGateway

            gateway = self.llm_gateway or LLMGateway()
            tools = self.tool_registry or AgenticToolRegistry(
                kb=self.kb,
                payroll_agent=self.payroll_agent,
                compliance_agent=self.compliance_agent,
                leave_agent=self.leave_agent,
                recruitment_agent=self.recruitment_agent,
                onboarding_agent=self.onboarding_agent,
                offboarding_agent=self.offboarding_agent,
                travel_agent=self.travel_agent,
            )
            self._orchestrator = AgenticOrchestrator(
                llm_gateway=gateway,
                tool_registry=tools,
                classification_for=self.classification_for,
            )
        return self._orchestrator

    async def process_agentic(
        self,
        message: str,
        jurisdiction: Jurisdiction | str = Jurisdiction.INDIA,
        context: Optional[Dict[str, Any]] = None,
        history: Optional[List[Dict[str, str]]] = None,
        *,
        auth: AuthContext,
    ) -> AgentResponse:
        """Execute autonomous ReAct tool loop for multi-step reasoning."""
        jur_val = jurisdiction.value if isinstance(jurisdiction, Jurisdiction) else str(jurisdiction)
        orchestrator = self.get_orchestrator()
        return await orchestrator.run(
            user_message=message,
            jurisdiction=jur_val,
            context=context,
            history=history,
            auth=auth,
        )

    async def stream_agentic(
        self,
        message: str,
        jurisdiction: Jurisdiction | str = Jurisdiction.INDIA,
        context: Optional[Dict[str, Any]] = None,
        history: Optional[List[Dict[str, str]]] = None,
        *,
        auth: AuthContext,
    ):
        """Stream real-time SSE events from autonomous ReAct loop."""
        jur_val = jurisdiction.value if isinstance(jurisdiction, Jurisdiction) else str(jurisdiction)
        orchestrator = self.get_orchestrator()
        async for chunk in orchestrator.run_stream(
            user_message=message,
            jurisdiction=jur_val,
            context=context,
            history=history,
            auth=auth,
        ):
            yield chunk
