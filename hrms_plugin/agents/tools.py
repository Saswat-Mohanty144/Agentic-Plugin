"""Enterprise Tool Registry for Agentic HRMS AI Plugin.

Exposes deterministic domain specialists as callable tools with strict JSON schemas.
Agents invoke these tools during the ReAct reasoning loop.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from hrms_plugin.agents.compliance import ComplianceAgent
from hrms_plugin.agents.leave_attendance import LeaveAttendanceAgent
from hrms_plugin.agents.offboarding import OffboardingAgent
from hrms_plugin.agents.onboarding import OnboardingAgent
from hrms_plugin.agents.recruitment import RecruitmentAgent
from hrms_plugin.agents.statutory_payroll import StatutoryPayrollAgent
from hrms_plugin.agents.travel_expense import TravelExpenseAgent
from hrms_plugin.ai.llm import ToolDefinition
from hrms_plugin.rag.store import Jurisdiction, StatutoryKnowledgeBase


class AgenticToolRegistry:
    """Registry managing available tools and their deterministic execution bindings."""

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
    ):
        self.kb = kb or StatutoryKnowledgeBase()
        self.payroll_agent = payroll_agent or StatutoryPayrollAgent(kb=self.kb)
        self.compliance_agent = compliance_agent or ComplianceAgent(kb=self.kb)
        self.leave_agent = leave_agent or LeaveAttendanceAgent()
        self.recruitment_agent = recruitment_agent or RecruitmentAgent()
        self.onboarding_agent = onboarding_agent or OnboardingAgent()
        self.offboarding_agent = offboarding_agent or OffboardingAgent(kb=self.kb, payroll_agent=self.payroll_agent)
        self.travel_agent = travel_agent or TravelExpenseAgent()

        self._tools: Dict[str, ToolDefinition] = {}
        self._handlers: Dict[str, Callable[..., Any]] = {}
        self._register_default_tools()

    def register(self, definition: ToolDefinition, handler: Callable[..., Any]) -> None:
        self._tools[definition.name] = definition
        self._handlers[definition.name] = handler

    def get_definitions(self) -> List[ToolDefinition]:
        return list(self._tools.values())

    async def execute(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a tool by name with arguments and return JSON-serializable output."""
        if tool_name not in self._handlers:
            return {"error": f"Tool '{tool_name}' is not registered."}
        try:
            handler = self._handlers[tool_name]
            result = handler(**arguments)
            # Normalize Pydantic models or dataclasses
            if hasattr(result, "model_dump"):
                return result.model_dump()
            elif isinstance(result, (dict, list, str, int, float, bool)):
                return result
            else:
                return {"result": str(result)}
        except Exception as e:
            return {"error": f"Tool execution failed for '{tool_name}': {str(e)}"}

    def _register_default_tools(self) -> None:
        # 1. Salary Structure Tool
        self.register(
            ToolDefinition(
                name="calculate_salary_structure",
                description="Calculate exact statutory gross-to-net salary and CTC breakdown for India or UAE.",
                parameters={
                    "type": "object",
                    "properties": {
                        "gross_or_ctc": {"type": "number", "description": "Annual CTC or Monthly Gross salary"},
                        "jurisdiction": {"type": "string", "enum": ["IN", "AE", "SA", "US"], "default": "IN"},
                    },
                    "required": ["gross_or_ctc"],
                },
            ),
            self._tool_calculate_salary,
        )

        # 2. EOSB Gratuity Tool
        self.register(
            ToolDefinition(
                name="calculate_eosb_gratuity",
                description=(
                    "Calculate End of Service Gratuity / Severance according to "
                    "UAE Labor Law Art. 51 or India Gratuity Act."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "basic_wage": {"type": "number", "description": "Monthly basic wage"},
                        "tenure_years": {"type": "number", "description": "Total completed years of service"},
                        "jurisdiction": {"type": "string", "enum": ["AE", "IN"], "default": "AE"},
                    },
                    "required": ["basic_wage", "tenure_years"],
                },
            ),
            self._tool_calculate_eosb,
        )

        # 3. Compliance Audit Tool
        self.register(
            ToolDefinition(
                name="audit_statutory_compliance",
                description=(
                    "Audit employee records against statutory labor codes "
                    "(probation limits, working hours, WPS, PF/ESI)."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "jurisdiction": {"type": "string", "enum": ["IN", "AE", "SA", "US"], "default": "AE"},
                        "employees": {
                            "type": "array",
                            "items": {"type": "object"},
                            "description": "List of employee dictionary records to audit.",
                        },
                        "tenant_id": {"type": "string", "default": "DEFAULT"},
                    },
                    "required": ["jurisdiction"],
                },
            ),
            self._tool_audit_compliance,
        )

        # 4. Search Labor Laws Tool
        self.register(
            ToolDefinition(
                name="search_labor_regulations",
                description="Search grounded statutory legal corpus for labor gazette articles and citations.",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query about labor law or statutory rule"},
                        "jurisdiction": {"type": "string", "enum": ["IN", "AE", "SA", "US"], "default": "AE"},
                        "limit": {"type": "integer", "default": 3},
                    },
                    "required": ["query"],
                },
            ),
            self._tool_search_laws,
        )

        # 5. Evaluate Leave Tool
        self.register(
            ToolDefinition(
                name="evaluate_leave_request",
                description="Evaluate employee leave request against current balance and company policy.",
                parameters={
                    "type": "object",
                    "properties": {
                        "employee_id": {"type": "string"},
                        "leave_type": {
                            "type": "string",
                            "enum": ["ANNUAL", "SICK", "CASUAL", "UNPAID"],
                            "default": "ANNUAL",
                        },
                        "requested_days": {"type": "number"},
                        "current_balance": {"type": "number", "default": 15.0},
                    },
                    "required": ["employee_id", "requested_days"],
                },
            ),
            self._tool_evaluate_leave,
        )

        # 6. Impossible Travel Anomaly Tool
        self.register(
            ToolDefinition(
                name="detect_travel_anomaly",
                description="Detect impossible-travel anomalies between two biometric attendance punches.",
                parameters={
                    "type": "object",
                    "properties": {
                        "punch1": {"type": "object", "description": "First punch with lat, lng, timestamp"},
                        "punch2": {"type": "object", "description": "Second punch with lat, lng, timestamp"},
                    },
                    "required": ["punch1", "punch2"],
                },
            ),
            self._tool_detect_travel,
        )

        # 7. ATS Resume Screening Tool
        self.register(
            ToolDefinition(
                name="score_candidate_ats",
                description="Screen candidate resume text against required skills and experience.",
                parameters={
                    "type": "object",
                    "properties": {
                        "resume_text": {"type": "string"},
                        "required_skills": {"type": "array", "items": {"type": "string"}},
                        "required_experience_years": {"type": "number", "default": 3.0},
                        "candidate_experience_years": {"type": "number", "default": 3.0},
                    },
                    "required": ["resume_text"],
                },
            ),
            self._tool_score_ats,
        )

        # 8. Job Description Bias Audit Tool
        self.register(
            ToolDefinition(
                name="audit_job_description_bias",
                description="Detect and remove gender or exclusionary bias from job descriptions.",
                parameters={
                    "type": "object",
                    "properties": {
                        "jd_text": {"type": "string", "description": "Text of the job description"},
                    },
                    "required": ["jd_text"],
                },
            ),
            self._tool_audit_bias,
        )

        # 9. Offer Evaluation Tool
        self.register(
            ToolDefinition(
                name="evaluate_onboarding_offer",
                description="Evaluate candidate offer against salary bands and statutory minimum wage.",
                parameters={
                    "type": "object",
                    "properties": {
                        "candidate_name": {"type": "string"},
                        "job_title": {"type": "string"},
                        "annual_salary": {"type": "number"},
                        "currency": {"type": "string", "default": "INR"},
                        "band_min": {"type": "number"},
                        "band_max": {"type": "number"},
                        "jurisdiction": {"type": "string", "enum": ["IN", "AE", "SA", "US"], "default": "IN"},
                    },
                    "required": ["candidate_name", "job_title", "annual_salary"],
                },
            ),
            self._tool_evaluate_offer,
        )

        # 10. Document Verification Tool
        self.register(
            ToolDefinition(
                name="verify_onboarding_documents",
                description="Verify statutory KYC documents for identity and social security compliance.",
                parameters={
                    "type": "object",
                    "properties": {
                        "candidate_name": {"type": "string"},
                        "jurisdiction": {"type": "string", "enum": ["IN", "AE", "SA", "US"], "default": "AE"},
                        "submitted_documents": {"type": "object", "description": "Dict of document key-values"},
                    },
                    "required": ["candidate_name", "jurisdiction", "submitted_documents"],
                },
            ),
            self._tool_verify_docs,
        )

        # 11. Offboarding FNF Timeline Tool
        self.register(
            ToolDefinition(
                name="audit_offboarding_fnf_timeline",
                description="Audit 14-day statutory final settlement deadline under UAE Labor Law Art. 53.",
                parameters={
                    "type": "object",
                    "properties": {
                        "employee_id": {"type": "string"},
                        "employee_name": {"type": "string"},
                        "last_working_day": {"type": "string", "description": "ISO date YYYY-MM-DD"},
                        "current_date": {"type": "string", "description": "ISO date YYYY-MM-DD"},
                    },
                    "required": ["employee_id", "employee_name", "last_working_day"],
                },
            ),
            self._tool_audit_fnf_timeline,
        )

        # 12. Final Settlement Statement Tool
        self.register(
            ToolDefinition(
                name="compute_final_settlement",
                description="Compute final settlement statement with leave encashment, gratuity, and notice pay.",
                parameters={
                    "type": "object",
                    "properties": {
                        "employee_id": {"type": "string"},
                        "employee_name": {"type": "string"},
                        "basic_wage_monthly": {"type": "number"},
                        "gross_wage_monthly": {"type": "number"},
                        "tenure_years": {"type": "number"},
                        "unused_leave_days": {"type": "number", "default": 0.0},
                        "unpaid_work_days": {"type": "integer", "default": 0},
                        "notice_days_to_pay": {"type": "integer", "default": 0},
                        "notice_days_to_deduct": {"type": "integer", "default": 0},
                        "jurisdiction": {"type": "string", "enum": ["AE", "IN"], "default": "AE"},
                    },
                    "required": [
                        "employee_id",
                        "employee_name",
                        "basic_wage_monthly",
                        "gross_wage_monthly",
                        "tenure_years",
                    ],
                },
            ),
            self._tool_compute_final_settlement,
        )

        # 13. Travel & Expense Audit Tool
        self.register(
            ToolDefinition(
                name="audit_travel_expense_claim",
                description="Audit business travel and lodging claims against per-diem caps and duplicate receipts.",
                parameters={
                    "type": "object",
                    "properties": {
                        "claim_id": {"type": "string"},
                        "employee_id": {"type": "string"},
                        "expenses": {"type": "array", "items": {"type": "object"}},
                        "currency": {"type": "string", "default": "INR"},
                        "tier": {"type": "string", "enum": ["METRO", "NON_METRO"], "default": "METRO"},
                        "has_approved_travel_request": {"type": "boolean", "default": True},
                    },
                    "required": ["claim_id", "employee_id", "expenses"],
                },
            ),
            self._tool_audit_expenses,
        )

    # Handler wrappers
    def _tool_calculate_salary(self, gross_or_ctc: float, jurisdiction: str = "IN") -> Dict[str, Any]:
        jur = Jurisdiction(jurisdiction) if jurisdiction in [j.value for j in Jurisdiction] else Jurisdiction.INDIA
        if jur == Jurisdiction.UAE:
            res = self.payroll_agent.structure_uae_salary(monthly_gross=float(gross_or_ctc))
            return res.model_dump()
        else:
            res = self.payroll_agent.structure_india_salary(annual_ctc=float(gross_or_ctc))
            return res.model_dump()

    def _tool_calculate_eosb(self, basic_wage: float, tenure_years: float, jurisdiction: str = "AE") -> Dict[str, Any]:
        jur = Jurisdiction(jurisdiction) if jurisdiction in [j.value for j in Jurisdiction] else Jurisdiction.UAE
        if jur == Jurisdiction.UAE:
            res = self.payroll_agent.calculate_uae_eosb(
                basic_wage_monthly=float(basic_wage), tenure_years=float(tenure_years)
            )
            return res.model_dump()
        else:
            res = self.payroll_agent.calculate_india_gratuity(
                last_drawn_basic=float(basic_wage), tenure_years=float(tenure_years)
            )
            return res.model_dump()

    def _tool_audit_compliance(
        self,
        jurisdiction: str = "AE",
        employees: Optional[List[Dict[str, Any]]] = None,
        tenant_id: str = "DEFAULT",
    ) -> Dict[str, Any]:
        jur = Jurisdiction(jurisdiction) if jurisdiction in [j.value for j in Jurisdiction] else Jurisdiction.UAE
        sample_emps = employees or [
            {
                "id": "EMP-101",
                "name": "Tariq Al-Mansoor",
                "basic_salary": 4500.0,
                "gross_salary": 12000.0,
                "probation_days": 210,
                "weekly_hours": 54,
                "uae_wps_registered": False,
            }
        ]
        res = self.compliance_agent.audit_employees(tenant_id=tenant_id, jurisdiction=jur, employees=sample_emps)
        return res.model_dump()

    def _tool_search_laws(self, query: str, jurisdiction: str = "AE", limit: int = 3) -> Dict[str, Any]:
        jur = Jurisdiction(jurisdiction) if jurisdiction in [j.value for j in Jurisdiction] else Jurisdiction.UAE
        results = self.kb.search(query=query, jurisdiction=jur, limit=limit)
        return {
            "results_count": len(results),
            "citations": [
                {
                    "title": c.title,
                    "section": c.section_or_article,
                    "summary": c.summary,
                    "citation": self.kb.format_citation(c),
                }
                for c in results
            ],
        }

    def _tool_evaluate_leave(
        self,
        employee_id: str,
        requested_days: float,
        leave_type: str = "ANNUAL",
        current_balance: float = 15.0,
    ) -> Dict[str, Any]:
        res = self.leave_agent.evaluate_leave_request(
            employee_id=employee_id,
            leave_type=leave_type,
            requested_days=float(requested_days),
            current_balance=float(current_balance),
        )
        return res.model_dump()

    def _tool_detect_travel(self, punch1: Dict[str, Any], punch2: Dict[str, Any]) -> Dict[str, Any]:
        res = self.leave_agent.detect_impossible_travel(punch1=punch1, punch2=punch2)
        return res.model_dump()

    def _tool_score_ats(
        self,
        resume_text: str,
        required_skills: Optional[List[str]] = None,
        required_experience_years: float = 3.0,
        candidate_experience_years: float = 3.0,
    ) -> Dict[str, Any]:
        skills = required_skills or ["Python", "FastAPI", "PostgreSQL", "Docker"]
        res = self.recruitment_agent.score_candidate(
            resume_text=resume_text,
            required_skills=skills,
            required_experience_years=float(required_experience_years),
            candidate_experience_years=float(candidate_experience_years),
        )
        return res.model_dump()

    def _tool_audit_bias(self, jd_text: str) -> Dict[str, Any]:
        res = self.recruitment_agent.audit_job_description_bias(jd_text)
        return res.model_dump()

    def _tool_evaluate_offer(
        self,
        candidate_name: str,
        job_title: str,
        annual_salary: float,
        currency: str = "INR",
        band_min: Optional[float] = None,
        band_max: Optional[float] = None,
        jurisdiction: str = "IN",
    ) -> Dict[str, Any]:
        jur = Jurisdiction(jurisdiction) if jurisdiction in [j.value for j in Jurisdiction] else Jurisdiction.INDIA
        res = self.onboarding_agent.evaluate_offer(
            candidate_name=candidate_name,
            job_title=job_title,
            annual_salary=annual_salary,
            currency=currency,
            band_min=band_min,
            band_max=band_max,
            jurisdiction=jur,
        )
        return res.model_dump()

    def _tool_verify_docs(
        self,
        candidate_name: str,
        jurisdiction: str,
        submitted_documents: Dict[str, str],
    ) -> Dict[str, Any]:
        jur = Jurisdiction(jurisdiction) if jurisdiction in [j.value for j in Jurisdiction] else Jurisdiction.UAE
        res = self.onboarding_agent.verify_onboarding_documents(
            candidate_name=candidate_name,
            jurisdiction=jur,
            submitted_documents=submitted_documents,
        )
        return res.model_dump()

    def _tool_audit_fnf_timeline(
        self,
        employee_id: str,
        employee_name: str,
        last_working_day: str,
        current_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        res = self.offboarding_agent.audit_uae_settlement_timeline(
            employee_id=employee_id,
            employee_name=employee_name,
            last_working_day=last_working_day,
            current_date_str=current_date,
        )
        return res.model_dump()

    def _tool_compute_final_settlement(
        self,
        employee_id: str,
        employee_name: str,
        basic_wage_monthly: float,
        gross_wage_monthly: float,
        tenure_years: float,
        unused_leave_days: float = 0.0,
        unpaid_work_days: int = 0,
        notice_days_to_pay: int = 0,
        notice_days_to_deduct: int = 0,
        jurisdiction: str = "AE",
    ) -> Dict[str, Any]:
        jur = Jurisdiction(jurisdiction) if jurisdiction in [j.value for j in Jurisdiction] else Jurisdiction.UAE
        res = self.offboarding_agent.compute_final_settlement(
            employee_id=employee_id,
            employee_name=employee_name,
            basic_wage_monthly=basic_wage_monthly,
            gross_wage_monthly=gross_wage_monthly,
            tenure_years=tenure_years,
            unused_leave_days=unused_leave_days,
            unpaid_work_days=unpaid_work_days,
            notice_days_to_pay=notice_days_to_pay,
            notice_days_to_deduct=notice_days_to_deduct,
            jurisdiction=jur,
        )
        return res.model_dump()

    def _tool_audit_expenses(
        self,
        claim_id: str,
        employee_id: str,
        expenses: List[Dict[str, Any]],
        currency: str = "INR",
        tier: str = "METRO",
        has_approved_travel_request: bool = True,
    ) -> Dict[str, Any]:
        res = self.travel_agent.audit_claim(
            claim_id=claim_id,
            employee_id=employee_id,
            expenses=expenses,
            currency=currency,
            tier=tier,
            has_approved_travel_request=has_approved_travel_request,
        )
        return res.model_dump()
