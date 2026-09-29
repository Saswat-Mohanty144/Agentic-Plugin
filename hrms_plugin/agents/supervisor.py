"""Cognitive Multi-Agent Supervisor: Intent Router & Orchestration Brain."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from hrms_plugin.agents.compliance import ComplianceAgent
from hrms_plugin.agents.leave_attendance import LeaveAttendanceAgent
from hrms_plugin.agents.offboarding import OffboardingAgent
from hrms_plugin.agents.onboarding import OnboardingAgent
from hrms_plugin.agents.recruitment import RecruitmentAgent
from hrms_plugin.agents.statutory_payroll import StatutoryPayrollAgent
from hrms_plugin.agents.travel_expense import TravelExpenseAgent
from hrms_plugin.rag.store import Jurisdiction, StatutoryKnowledgeBase


class UserIntent(str, Enum):
    PAYROLL_STRUCTURE = "PAYROLL_STRUCTURE"
    PAYROLL_EOSB = "PAYROLL_EOSB"
    COMPLIANCE_AUDIT = "COMPLIANCE_AUDIT"
    LEAVE_APPLY = "LEAVE_APPLY"
    ATTENDANCE_ANOMALY = "ATTENDANCE_ANOMALY"
    RECRUITMENT_ATS = "RECRUITMENT_ATS"
    RECRUITMENT_BIAS = "RECRUITMENT_BIAS"
    ONBOARDING = "ONBOARDING"
    OFFBOARDING_FNF = "OFFBOARDING_FNF"
    TRAVEL_EXPENSE = "TRAVEL_EXPENSE"
    LEGAL_QNA = "LEGAL_QNA"
    GENERAL_HR = "GENERAL_HR"


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
        self._orchestrator = None

    def classify_intent(self, message: str) -> tuple[UserIntent, float]:
        """Classify user intent using deterministic keyword intent heuristic with confidence scoring."""
        msg = message.lower()

        # EOSB / Gratuity
        if any(w in msg for w in ["eosb", "end of service", "gratuity calculation", "severance"]):
            return UserIntent.PAYROLL_EOSB, 0.95

        # Salary & CTC structuring
        payroll_keywords = [
            "salary structure",
            "ctc breakdown",
            "calculate salary",
            "take home",
            "gross to net",
            "pf deduction",
        ]
        if any(w in msg for w in payroll_keywords):
            return UserIntent.PAYROLL_STRUCTURE, 0.92

        # Compliance audits
        compliance_keywords = [
            "compliance",
            "labor law",
            "wps audit",
            "violation",
            "statutory audit",
            "probation limit",
        ]
        if any(w in msg for w in compliance_keywords):
            return UserIntent.COMPLIANCE_AUDIT, 0.90

        # Leave application & balance
        if any(w in msg for w in ["apply leave", "leave balance", "vacation request", "sick leave", "casual leave"]):
            return UserIntent.LEAVE_APPLY, 0.92

        # Attendance & travel anomaly
        anomaly_keywords = [
            "impossible travel",
            "punch anomaly",
            "check-in location",
            "ghost punch",
            "attendance fraud",
        ]
        if any(w in msg for w in anomaly_keywords):
            return UserIntent.ATTENDANCE_ANOMALY, 0.94

        # ATS candidate scoring
        if any(w in msg for w in ["score resume", "ats score", "match candidate", "screen applicant"]):
            return UserIntent.RECRUITMENT_ATS, 0.91

        # JD Bias check
        if any(w in msg for w in ["bias", "inclusive jd", "gender neutral", "audit job description"]):
            return UserIntent.RECRUITMENT_BIAS, 0.93

        # Onboarding & Pre-hire
        onboarding_keywords = [
            "onboard",
            "offer letter",
            "pre-hire",
            "verify kyc",
            "pan verification",
            "aadhaar check",
            "emirates id",
            "it provisioning",
            "new hire",
        ]
        if any(w in msg for w in onboarding_keywords):
            return UserIntent.ONBOARDING, 0.92

        # Offboarding & Final Settlement (FNF)
        offboarding_keywords = [
            "offboard",
            "fnf",
            "final settlement",
            "settlement",
            "resignation",
            "notice period buyout",
            "relieving letter",
            "exit clearance",
            "last working day",
            "article 53",
        ]
        if any(w in msg for w in offboarding_keywords):
            return UserIntent.OFFBOARDING_FNF, 0.93

        # Travel & Expense Reimbursement
        travel_keywords = [
            "travel expense",
            "reimbursement claim",
            "per diem",
            "per-diem",
            "expense audit",
            "hotel claim",
            "flight expense",
            "meal allowance",
        ]
        if any(w in msg for w in travel_keywords):
            return UserIntent.TRAVEL_EXPENSE, 0.92

        # Legal QnA
        if any(w in msg for w in ["labor code", "section", "article", "gazette", "overtime rule", "maternity law"]):
            return UserIntent.LEGAL_QNA, 0.88

        return UserIntent.GENERAL_HR, 0.70

    def process_message(
        self,
        message: str,
        jurisdiction: Jurisdiction | str = Jurisdiction.INDIA,
        context: Optional[Dict[str, Any]] = None,
    ) -> AgentResponse:
        """Route message to appropriate specialist agent and generate unified response."""
        intent, confidence = self.classify_intent(message)
        jur = Jurisdiction(jurisdiction) if isinstance(jurisdiction, str) else jurisdiction
        ctx = context or {}
        msg = message.lower()

        # 1. Statutory Salary Structuring
        if intent == UserIntent.PAYROLL_STRUCTURE:
            ctc = float(ctx.get("annual_ctc") or ctx.get("ctc") or 1200000.0)
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
            basic = float(ctx.get("basic_wage_monthly") or ctx.get("basic") or 15000.0)
            tenure = float(ctx.get("tenure_years") or ctx.get("tenure") or 3.5)
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
            tenant_id = str(ctx.get("tenant_id", "TENANT-01"))
            employees = ctx.get("employees", [])
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
                    {"action": "REMEDIATE_PATCH", "violation_id": v.violation_id, "patch": v.remediation_patch}
                    for v in audit_res.violations
                    if v.remediation_patch
                ],
            )

        # 4. Leave Application
        elif intent == UserIntent.LEAVE_APPLY:
            emp_id = str(ctx.get("employee_id", "EMP-001"))
            leave_type = str(ctx.get("leave_type", "ANNUAL"))
            req_days = float(ctx.get("requested_days", 2.0))
            cur_bal = float(ctx.get("current_balance", 10.0))
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
            p1 = ctx.get("punch1", {"lat": 19.0760, "lng": 72.8777, "timestamp": "2026-09-24T09:00:00Z"})
            p2 = ctx.get("punch2", {"lat": 25.2048, "lng": 55.2708, "timestamp": "2026-09-24T10:00:00Z"})
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
            resume = str(ctx.get("resume_text", message))
            skills = ctx.get("required_skills", ["Python", "FastAPI", "PostgreSQL", "Docker"])
            req_exp = float(ctx.get("required_experience_years", 3.0))
            cand_exp = float(ctx.get("candidate_experience_years", 4.0))
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
            cand_name = str(ctx.get("candidate_name", "Jane Doe"))
            if any(k in msg for k in ["document", "kyc", "pan", "aadhaar", "emirates id"]):
                docs = ctx.get("documents", {})
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
                emp_id = str(ctx.get("employee_id", "EMP-NEW"))
                dept = str(ctx.get("department", "Engineering"))
                role = str(ctx.get("role", "Software Engineer"))
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
                title = str(ctx.get("job_title", "Senior Specialist"))
                salary = float(ctx.get("annual_salary", 1500000.0))
                currency = str(ctx.get("currency", "INR" if jur == Jurisdiction.INDIA else "AED"))
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
            emp_id = str(ctx.get("employee_id", "EMP-EXIT"))
            emp_name = str(ctx.get("employee_name", "John Exit"))
            if any(k in msg for k in ["timeline", "article 53", "deadline", "14-day"]):
                lwd = str(ctx.get("last_working_day", "2026-09-20"))
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
                dept = str(ctx.get("department", "Engineering"))
                returned = ctx.get("returned_assets", ["Laptop"])
                pending = ctx.get("pending_assets", [])
                fin_cleared = bool(ctx.get("finance_cleared", True))
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
                basic = float(ctx.get("basic_wage_monthly", 12000.0))
                gross = float(ctx.get("gross_wage_monthly", 20000.0))
                tenure = float(ctx.get("tenure_years", 3.0))
                leave_days = float(ctx.get("unused_leave_days", 5.0))
                unpaid_days = int(ctx.get("unpaid_work_days", 10))
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
            claim_id = str(ctx.get("claim_id", "CLM-101"))
            emp_id = str(ctx.get("employee_id", "EMP-TRV"))
            expenses = ctx.get(
                "expenses",
                [
                    {"id": "EXP-1", "category": "MEAL", "amount": "1500.00"},
                ],
            )
            cur_code = str(ctx.get("currency", "INR" if jur == Jurisdiction.INDIA else "AED"))
            tier = str(ctx.get("tier", "METRO"))
            has_pre_auth = bool(ctx.get("has_approved_travel_request", True))
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
            self._orchestrator = AgenticOrchestrator(llm_gateway=gateway, tool_registry=tools)
        return self._orchestrator

    async def process_agentic(
        self,
        message: str,
        jurisdiction: Jurisdiction | str = Jurisdiction.INDIA,
        context: Optional[Dict[str, Any]] = None,
        tenant_id: str = "DEFAULT",
        history: Optional[List[Dict[str, str]]] = None,
    ) -> AgentResponse:
        """Execute autonomous ReAct tool loop for multi-step reasoning."""
        jur_val = jurisdiction.value if isinstance(jurisdiction, Jurisdiction) else str(jurisdiction)
        orchestrator = self.get_orchestrator()
        return await orchestrator.run(
            user_message=message,
            jurisdiction=jur_val,
            context=context,
            tenant_id=tenant_id,
            history=history,
        )

    async def stream_agentic(
        self,
        message: str,
        jurisdiction: Jurisdiction | str = Jurisdiction.INDIA,
        context: Optional[Dict[str, Any]] = None,
        tenant_id: str = "DEFAULT",
    ):
        """Stream real-time SSE events from autonomous ReAct loop."""
        jur_val = jurisdiction.value if isinstance(jurisdiction, Jurisdiction) else str(jurisdiction)
        orchestrator = self.get_orchestrator()
        async for chunk in orchestrator.run_stream(
            user_message=message,
            jurisdiction=jur_val,
            context=context,
            tenant_id=tenant_id,
        ):
            yield chunk
