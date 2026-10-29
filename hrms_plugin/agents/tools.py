"""Enterprise Tool Registry for Agentic HRMS AI Plugin.

Exposes deterministic domain specialists as callable tools with strict JSON schemas.
Agents invoke these tools during the ReAct reasoning loop.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable, Dict, List, Optional, Tuple

from hrms_plugin.agents.compliance import ComplianceAgent
from hrms_plugin.agents.leave_attendance import LeaveAttendanceAgent
from hrms_plugin.agents.offboarding import OffboardingAgent
from hrms_plugin.agents.onboarding import OnboardingAgent
from hrms_plugin.agents.recruitment import RecruitmentAgent
from hrms_plugin.agents.statutory_payroll import StatutoryPayrollAgent
from hrms_plugin.agents.supervisor import UserIntent
from hrms_plugin.agents.travel_expense import TravelExpenseAgent
from hrms_plugin.ai.llm import ToolDefinition
from hrms_plugin.rag.store import Jurisdiction, StatutoryKnowledgeBase
from hrms_plugin.security.rbac import AuthContext, Permission, RbacPolicyEnforcer

# The statutory agents implement India and UAE law only; SA/US have regulation search, never calculations (P3/P4).
STATUTORY_JURISDICTIONS = ["AE", "IN"]
# No-LLM-Numbers (QA): every number these tools calculate from must be one the user stated or the host context
# supplied; the orchestrator refuses the call otherwise. Nested parameters are checked value by value.
USER_STATED_PARAMS: Dict[str, Tuple[str, ...]] = {
    "calculate_salary_structure": ("gross_or_ctc",),
    "calculate_eosb_gratuity": ("basic_wage", "tenure_years"),
    "audit_statutory_compliance": ("employees",),
    "evaluate_leave_request": ("requested_days", "current_balance"),
    "detect_travel_anomaly": ("punch1", "punch2"),
    "score_candidate_ats": ("required_experience_years", "candidate_experience_years"),
    "evaluate_onboarding_offer": ("annual_salary", "band_min", "band_max"),
    "compute_final_settlement": ("basic_wage_monthly", "gross_wage_monthly", "tenure_years", "unused_leave_days",
                                 "unpaid_work_days", "notice_days_to_pay", "notice_days_to_deduct"),
    "audit_travel_expense_claim": ("expenses",),
}
HOST_RECORD_TOOLS: Tuple[str, ...] = ("calculate_pf_contribution",)  # inputs fetched from the host by ID
_JSON_TYPES: Dict[str, tuple] = {"string": (str,), "number": (int, float), "integer": (int,), "boolean": (bool,),
                                 "object": (dict,), "array": (list,)}


def check_arguments(schema: Dict[str, Any], arguments: Dict[str, Any]) -> List[str]:
    """P5: the LLM's arguments must match the tool's declared schema; nothing is coerced or assumed."""
    properties = schema.get("properties", {})
    problems = [f"missing:{name}" for name in schema.get("required", []) if name not in arguments]
    for name, value in arguments.items():
        spec = properties.get(name)
        if spec is None:
            problems.append(f"unknown:{name}")
            continue
        expected = spec.get("type")
        if expected and (not isinstance(value, _JSON_TYPES[expected])
                         or (isinstance(value, bool) and expected != "boolean")):
            problems.append(f"type:{name}")
            continue
        if "enum" in spec and value not in spec["enum"]:
            problems.append(f"enum:{name}")
        if "minItems" in spec and len(value) < spec["minItems"]:
            problems.append(f"min_items:{name}")
    return problems


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
        payroll_service: Optional[Any] = None,
    ):
        self.kb = kb or StatutoryKnowledgeBase()
        self.payroll_agent = payroll_agent or StatutoryPayrollAgent(kb=self.kb)
        self.compliance_agent = compliance_agent or ComplianceAgent(kb=self.kb)
        self.leave_agent = leave_agent or LeaveAttendanceAgent()
        self.recruitment_agent = recruitment_agent or RecruitmentAgent()
        self.onboarding_agent = onboarding_agent or OnboardingAgent()
        self.offboarding_agent = offboarding_agent or OffboardingAgent(kb=self.kb, payroll_agent=self.payroll_agent)
        self.travel_agent = travel_agent or TravelExpenseAgent()
        self._payroll_service = payroll_service

        self._tools: Dict[str, ToolDefinition] = {}
        self._handlers: Dict[str, Callable[..., Any]] = {}
        self._intents: Dict[str, UserIntent] = {}
        self._permissions: Dict[str, Permission] = {}
        self._rbac = RbacPolicyEnforcer()
        self._register_default_tools()

    def register(
        self, definition: ToolDefinition, handler: Callable[..., Any], *, intent: UserIntent, permission: Permission
    ) -> None:
        self._tools[definition.name] = definition
        self._handlers[definition.name] = handler
        self._intents[definition.name] = intent
        self._permissions[definition.name] = permission

    def permission_for(self, name: str) -> Optional[Permission]:
        return self._permissions.get(name)

    def is_permitted(self, name: str, auth: AuthContext) -> bool:
        permission = self._permissions.get(name)
        return permission is not None and self._rbac.is_authorized(auth, permission)

    def definitions_for(self, auth: AuthContext) -> List[ToolDefinition]:
        # Sorted: tools render first in the prompt, so their order is part of the cache key (piece H2).
        return [d for name, d in sorted(self._tools.items()) if self.is_permitted(name, auth)]

    def get_definition(self, name: str) -> Optional[ToolDefinition]:
        return self._tools.get(name)

    def intent_for(self, name: str) -> UserIntent:
        return self._intents.get(name, UserIntent.GENERAL_HR)

    @staticmethod
    def user_stated_params(name: str) -> Tuple[str, ...]:
        return USER_STATED_PARAMS.get(name, ())

    @staticmethod
    def input_source(name: str) -> Optional[str]:
        if name in HOST_RECORD_TOOLS:
            return "HOST_RECORD"
        return "USER_STATED" if name in USER_STATED_PARAMS else None

    def get_definitions(self) -> List[ToolDefinition]:
        return list(self._tools.values())

    async def execute(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a tool by name with arguments and return JSON-serializable output."""
        if tool_name not in self._handlers:
            return {"error": f"Tool '{tool_name}' is not registered."}
        schema = self._tools[tool_name].parameters
        supported = schema.get("properties", {}).get("jurisdiction", {}).get("enum")
        if supported and "jurisdiction" in arguments and arguments["jurisdiction"] not in supported:
            return {"success": False, "error": "JURISDICTION_NOT_SUPPORTED",
                    "jurisdiction": arguments["jurisdiction"], "supported": sorted(supported)}
        problems = check_arguments(schema, arguments)
        if not problems and tool_name == "compute_final_settlement":
            if arguments["basic_wage_monthly"] > arguments["gross_wage_monthly"]:  # canonical cross-check (QB)
                problems = ["basic_wage_monthly>gross_wage_monthly"]
        if problems:
            return {"success": False, "error": "INVALID_ARGUMENTS", "problems": problems}
        try:
            handler = self._handlers[tool_name]
            result = handler(**arguments)
            if inspect.isawaitable(result):
                result = await result
            # Normalize Pydantic models or dataclasses
            if hasattr(result, "model_dump"):
                result = result.model_dump()
            source = self.input_source(tool_name)
            if source and isinstance(result, dict) and "error" not in result and result.get("success") is not False:
                result = {**result, "input_source": source}  # where the figures came from (QA)
            if isinstance(result, (dict, list, str, int, float, bool)):
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
                        "jurisdiction": {"type": "string", "enum": STATUTORY_JURISDICTIONS},
                    },
                    "required": ["gross_or_ctc", "jurisdiction"],
                },
            ),
            self._tool_calculate_salary,
            intent=UserIntent.PAYROLL_STRUCTURE,
            permission=Permission.VIEW_SALARY,
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
                        "jurisdiction": {"type": "string", "enum": STATUTORY_JURISDICTIONS},
                    },
                    "required": ["basic_wage", "tenure_years", "jurisdiction"],
                },
            ),
            self._tool_calculate_eosb,
            intent=UserIntent.PAYROLL_EOSB,
            permission=Permission.VIEW_SALARY,
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
                        "jurisdiction": {"type": "string", "enum": STATUTORY_JURISDICTIONS},
                        "employees": {
                            "type": "array",
                            "items": {"type": "object"},
                            "minItems": 1,
                            "description": "List of employee dictionary records to audit.",
                        },
                        "tenant_id": {"type": "string"},
                    },
                    "required": ["jurisdiction", "employees"],
                },
            ),
            self._tool_audit_compliance,
            intent=UserIntent.COMPLIANCE_AUDIT,
            permission=Permission.VIEW_COMPLIANCE,
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
                        "jurisdiction": {"type": "string", "enum": ["IN", "AE", "SA", "US"]},
                        "limit": {"type": "integer", "default": 3},
                    },
                    "required": ["query", "jurisdiction"],
                },
            ),
            self._tool_search_laws,
            intent=UserIntent.LEGAL_QNA,
            permission=Permission.USE_COPILOT,
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
                        "current_balance": {"type": "number"},
                    },
                    "required": ["employee_id", "requested_days", "current_balance"],
                },
            ),
            self._tool_evaluate_leave,
            intent=UserIntent.LEAVE_APPLY,
            permission=Permission.APPLY_LEAVE,
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
            intent=UserIntent.ATTENDANCE_ANOMALY,
            permission=Permission.APPROVE_LEAVE,
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
                        "required_skills": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                        "required_experience_years": {"type": "number"},
                        "candidate_experience_years": {"type": "number"},
                    },
                    "required": [
                        "resume_text", "required_skills", "required_experience_years", "candidate_experience_years"
                    ],
                },
            ),
            self._tool_score_ats,
            intent=UserIntent.RECRUITMENT_ATS,
            permission=Permission.MANAGE_RECRUITMENT,
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
            intent=UserIntent.RECRUITMENT_BIAS,
            permission=Permission.MANAGE_RECRUITMENT,
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
                        "jurisdiction": {"type": "string", "enum": STATUTORY_JURISDICTIONS},
                    },
                    "required": ["candidate_name", "job_title", "annual_salary", "jurisdiction"],
                },
            ),
            self._tool_evaluate_offer,
            intent=UserIntent.ONBOARDING,
            permission=Permission.MANAGE_RECRUITMENT,
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
                        "jurisdiction": {"type": "string", "enum": STATUTORY_JURISDICTIONS},
                        "submitted_documents": {"type": "object", "description": "Dict of document key-values"},
                    },
                    "required": ["candidate_name", "jurisdiction", "submitted_documents"],
                },
            ),
            self._tool_verify_docs,
            intent=UserIntent.ONBOARDING,
            permission=Permission.VIEW_EMPLOYEE,
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
            intent=UserIntent.OFFBOARDING_FNF,
            permission=Permission.VIEW_SALARY,
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
                        "jurisdiction": {"type": "string", "enum": STATUTORY_JURISDICTIONS},
                    },
                    "required": [
                        "employee_id",
                        "employee_name",
                        "basic_wage_monthly",
                        "gross_wage_monthly",
                        "tenure_years",
                        "jurisdiction",
                    ],
                },
            ),
            self._tool_compute_final_settlement,
            intent=UserIntent.OFFBOARDING_FNF,
            permission=Permission.VIEW_SALARY,
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
            intent=UserIntent.TRAVEL_EXPENSE,
            permission=Permission.APPROVE_EXPENSE,
        )

        # 15. PF contribution from the host's submitted Salary Slip (piece F1). IDs only — never amounts.
        self.register(
            ToolDefinition(
                name="calculate_pf_contribution",
                description=(
                    "Calculate Indian EPF/EPS contributions for one employee and month from the host HRMS's "
                    "submitted salary slip, using this tenant's approved payroll rules. Takes only the employee ID "
                    "and the month; never pass salary amounts."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "employee_id": {"type": "string", "description": "Host employee ID, e.g. HR-EMP-00001"},
                        "period": {"type": "string", "description": "Payroll month as YYYY-MM"},
                        "jurisdiction": {"type": "string", "enum": ["IN", "AE", "SA", "US"]},
                        "tenant_id": {"type": "string"},
                    },
                    "required": ["employee_id", "period", "jurisdiction"],
                },
            ),
            self._tool_calculate_pf,
            intent=UserIntent.PAYROLL_STRUCTURE,
            permission=Permission.VIEW_SALARY,
        )

        # 14. Smart Requisition 4-Archetype Generator Tool
        self.register(
            ToolDefinition(
                name="generate_requisition_archetypes",
                description=(
                    "Generate 4 dynamic candidate and role archetypes (Delivery, Architecture, "
                    "Specialization, Leadership) for a job requisition with salary bands, skills, "
                    "and responsibilities from a hiring prompt."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": (
                                "Natural language hiring query e.g. "
                                "'Lead Data Engineer in Bangalore with budget 35-45 LPA'"
                            ),
                        },
                        "location": {
                            "type": "string",
                            "description": "Target job location e.g. 'Bangalore, India' or 'Dubai, UAE'",
                        },
                    },
                    "required": ["prompt"],
                },
            ),
            self._tool_generate_archetypes,
            intent=UserIntent.RECRUITMENT_REQUISITION,
            permission=Permission.MANAGE_RECRUITMENT,
        )

    # Handler wrappers
    def _tool_calculate_salary(self, gross_or_ctc: float, jurisdiction: str) -> Dict[str, Any]:
        jur = Jurisdiction(jurisdiction)
        if jur == Jurisdiction.UAE:
            res = self.payroll_agent.structure_uae_salary(monthly_gross=float(gross_or_ctc))
            return res.model_dump()
        else:
            res = self.payroll_agent.structure_india_salary(annual_ctc=float(gross_or_ctc))
            return res.model_dump()

    def _tool_calculate_eosb(self, basic_wage: float, tenure_years: float, jurisdiction: str) -> Dict[str, Any]:
        jur = Jurisdiction(jurisdiction)
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

    @property
    def payroll_service(self) -> Any:
        if self._payroll_service is None:
            import os
            from pathlib import Path

            from hrms_plugin.payroll.service import PayrollService
            from hrms_plugin.storage.sqlite_store import SqliteKeyValueStore

            store = SqliteKeyValueStore(Path(os.environ.get("HRMS_DATA_DIR", ".hrms_data")) / "plugin_storage.db")
            self._payroll_service = PayrollService(store=store)
        return self._payroll_service

    async def _tool_calculate_pf(
        self, employee_id: str, period: str, *, jurisdiction: str, tenant_id: str = "DEFAULT"
    ) -> Dict[str, Any]:
        return await self.payroll_service.calculate_pf(tenant_id, employee_id, period, jurisdiction)

    def _tool_audit_compliance(
        self,
        jurisdiction: str,
        employees: List[Dict[str, Any]],
        tenant_id: str = "DEFAULT",
    ) -> Dict[str, Any]:
        jur = Jurisdiction(jurisdiction)
        res = self.compliance_agent.audit_employees(tenant_id=tenant_id, jurisdiction=jur, employees=employees)
        return res.model_dump()

    def _tool_search_laws(self, query: str, jurisdiction: str, limit: int = 3) -> Dict[str, Any]:
        jur = Jurisdiction(jurisdiction)
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
        current_balance: float,
        leave_type: str = "ANNUAL",
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
        required_skills: List[str],
        required_experience_years: float,
        candidate_experience_years: float,
    ) -> Dict[str, Any]:
        res = self.recruitment_agent.score_candidate(
            resume_text=resume_text,
            required_skills=required_skills,
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
        *,
        jurisdiction: str,
    ) -> Dict[str, Any]:
        jur = Jurisdiction(jurisdiction)
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
        jur = Jurisdiction(jurisdiction)
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
        *,
        jurisdiction: str,
    ) -> Dict[str, Any]:
        jur = Jurisdiction(jurisdiction)
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

    def _tool_generate_archetypes(self, prompt: str, location: Optional[str] = None) -> Dict[str, Any]:
        """Synthesize job requisition archetypes in-process.

        The former localhost:8001 call was unauthenticated, blocked the event loop and could hand the prompt to any
        local listener; in a deployment it always fell back to this path anyway (E2E review).
        """
        prompt = (prompt or "").strip()
        if not prompt:
            return {"success": False, "error": "EMPTY_PROMPT"}
        role_base = "Lead Data Engineer" if "data" in prompt.lower() else prompt.split()[0]
        fallback_archetypes = [
            {
                "id": "archetype_delivery",
                "name": f"{role_base} - Hands-on Delivery & Pipeline Execution",
                "badge": "Core Delivery",
                "tagline": (
                    "Specialized in robust production pipelines, ETL/ELT architecture, and rapid sprint execution."
                ),
                "experience": "6-10 yrs",
                "salary_range": None,
                "jobData": {
                    "title": f"{role_base} (Pipeline Delivery)",
                    "minSalary": None,
                    "maxSalary": None,
                    "salary": None,
                    "currency": "INR",
                    "minExperience": 6,
                    "maxExperience": 10,
                    "type": "Full-time",
                    "jobMode": "Hybrid",
                    "location": location or "Bangalore, India",
                    "jobDescription": (
                        f"We are hiring a {role_base} to build and optimize distributed data pipelines, "
                        "ETL flows, and real-time streaming infrastructure."
                    ),
                    "skills": ["Apache Spark", "Airflow", "Python", "SQL", "Kafka"],
                },
                "skills": ["Apache Spark", "Airflow", "Python", "SQL", "Kafka"],
            },
            {
                "id": "archetype_architecture",
                "name": f"{role_base} - Distributed Architecture & Cloud Lakehouse",
                "badge": "Architecture & Scale",
                "tagline": (
                    "Architecting resilient Petabyte-scale data mesh, low-latency storage, "
                    "and multi-cloud data platforms."
                ),
                "experience": "8-12 yrs",
                "salary_range": None,
                "jobData": {
                    "title": f"{role_base} (Cloud Lakehouse Architect)",
                    "minSalary": None,
                    "maxSalary": None,
                    "salary": None,
                    "currency": "INR",
                    "minExperience": 8,
                    "maxExperience": 12,
                    "type": "Full-time",
                    "jobMode": "Hybrid",
                    "location": location or "Bangalore, India",
                    "jobDescription": (
                        "Architect Petabyte-scale lakehouses, cross-region replication, "
                        "and modern data governance for enterprise workloads."
                    ),
                    "skills": ["Databricks", "Snowflake", "Delta Lake", "Distributed Systems", "Terraform"],
                },
                "skills": ["Databricks", "Snowflake", "Delta Lake", "Distributed Systems", "Terraform"],
            },
            {
                "id": "archetype_leadership",
                "name": f"{role_base} - Engineering Team Lead & Data Governance",
                "badge": "Leadership & Strategy",
                "tagline": (
                    "Driving data engineering squads, governance, SLA compliance, and cross-functional alignment."
                ),
                "experience": "8-14 yrs",
                "salary_range": None,
                "jobData": {
                    "title": f"{role_base} (Squad Lead & Governance)",
                    "minSalary": None,
                    "maxSalary": None,
                    "salary": None,
                    "currency": "INR",
                    "minExperience": 8,
                    "maxExperience": 14,
                    "type": "Full-time",
                    "jobMode": "Hybrid",
                    "location": location or "Bangalore, India",
                    "jobDescription": (
                        "Lead engineering squads, enforce data governance and quality SLAs, "
                        "and partner with product managers to deliver data initiatives."
                    ),
                    "skills": ["Team Leadership", "Data Governance", "Agile / Scrum", "Data Architecture"],
                },
                "skills": ["Team Leadership", "Data Governance", "Agile / Scrum", "Data Architecture"],
            },
        ]

        actions = [
            {
                "action_type": "APPLY_ARCHETYPE",
                "name": a["name"],
                "badge": a["badge"],
                "label": f"⚡ Auto-Fill Form with {a['badge']}",
                "archetype": a,
            }
            for a in fallback_archetypes
        ]

        return {
            "success": True,
            "prompt": prompt,
            "source": "TEMPLATE",  # not generated; pay, validity and benefits are left for the recruiter (P8)
            "requires_review": True,
            "total_archetypes": len(fallback_archetypes),
            "archetypes": fallback_archetypes,
            "suggested_actions": actions,
        }

