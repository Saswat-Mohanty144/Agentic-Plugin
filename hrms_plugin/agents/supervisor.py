"""Cognitive Multi-Agent Supervisor: Intent Router & Orchestration Brain."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from hrms_plugin.agents.compliance import ComplianceAgent
from hrms_plugin.agents.leave_attendance import LeaveAttendanceAgent
from hrms_plugin.agents.recruitment import RecruitmentAgent
from hrms_plugin.agents.statutory_payroll import StatutoryPayrollAgent
from hrms_plugin.rag.store import Jurisdiction, StatutoryKnowledgeBase


class UserIntent(str, Enum):
    PAYROLL_STRUCTURE = "PAYROLL_STRUCTURE"
    PAYROLL_EOSB = "PAYROLL_EOSB"
    COMPLIANCE_AUDIT = "COMPLIANCE_AUDIT"
    LEAVE_APPLY = "LEAVE_APPLY"
    ATTENDANCE_ANOMALY = "ATTENDANCE_ANOMALY"
    RECRUITMENT_ATS = "RECRUITMENT_ATS"
    RECRUITMENT_BIAS = "RECRUITMENT_BIAS"
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


class SupervisorAgent:
    """Central supervisor coordinating domain specialists and statutory legal RAG."""

    def __init__(
        self,
        kb: Optional[StatutoryKnowledgeBase] = None,
        payroll_agent: Optional[StatutoryPayrollAgent] = None,
        compliance_agent: Optional[ComplianceAgent] = None,
        leave_agent: Optional[LeaveAttendanceAgent] = None,
        recruitment_agent: Optional[RecruitmentAgent] = None,
    ):
        self.kb = kb or StatutoryKnowledgeBase()
        self.payroll_agent = payroll_agent or StatutoryPayrollAgent(kb=self.kb)
        self.compliance_agent = compliance_agent or ComplianceAgent(kb=self.kb)
        self.leave_agent = leave_agent or LeaveAttendanceAgent()
        self.recruitment_agent = recruitment_agent or RecruitmentAgent()

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

        # 8. Legal Knowledge RAG
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

        # 9. General HR Fallback
        return AgentResponse(
            intent=UserIntent.GENERAL_HR,
            routed_agent="SupervisorAgent",
            reply_text=(
                f"Received HR inquiry: '{message}'. How can I assist with Payroll, "
                "Compliance, Leave, Attendance, or Recruitment?"
            ),
            confidence_score=confidence,
        )
