"""Model Context Protocol (MCP) Server for the Agentic HRMS AI Plugin."""

from __future__ import annotations

from typing import Any, Dict, List

from pydantic import BaseModel

from hrms_plugin.agents.compliance import ComplianceAgent
from hrms_plugin.agents.leave_attendance import LeaveAttendanceAgent
from hrms_plugin.agents.recruitment import RecruitmentAgent
from hrms_plugin.agents.statutory_payroll import StatutoryPayrollAgent
from hrms_plugin.rag.store import StatutoryKnowledgeBase


class McpToolDefinition(BaseModel):
    name: str
    description: str
    input_schema: Dict[str, Any]


class HrmsMcpServer:
    """Provides standard MCP tool definitions and handlers for AI assistants."""

    def __init__(self):
        self.kb = StatutoryKnowledgeBase()
        self.payroll = StatutoryPayrollAgent(kb=self.kb)
        self.compliance = ComplianceAgent(kb=self.kb)
        self.leave = LeaveAttendanceAgent()
        self.recruitment = RecruitmentAgent()

    def list_tools(self) -> List[McpToolDefinition]:
        """Return registered MCP tools."""
        return [
            McpToolDefinition(
                name="hrms_query_labor_statutes",
                description=(
                    "Search statutory labor laws and legal gazette citations across India, UAE, Saudi Arabia, and US."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Legal topic, e.g. overtime, maternity, probation, gratuity",
                        },
                        "jurisdiction": {"type": "string", "enum": ["IN", "AE", "SA", "US"], "default": "IN"},
                    },
                    "required": ["query"],
                },
            ),
            McpToolDefinition(
                name="hrms_calculate_payroll_ctc",
                description="Calculate decimal-exact gross-to-net salary breakdown under India/UAE labor codes.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "annual_ctc": {"type": "number", "description": "Annual CTC in local currency"},
                        "jurisdiction": {"type": "string", "enum": ["IN", "AE"], "default": "IN"},
                        "basic_percent": {"type": "number", "default": 40.0},
                    },
                    "required": ["annual_ctc"],
                },
            ),
            McpToolDefinition(
                name="hrms_calculate_uae_eosb",
                description="Calculate End of Service Severance Gratuity (EOSB) under UAE Article 51.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "basic_wage_monthly": {"type": "number", "description": "Monthly basic wage in AED"},
                        "tenure_years": {"type": "number", "description": "Completed years of service"},
                    },
                    "required": ["basic_wage_monthly", "tenure_years"],
                },
            ),
            McpToolDefinition(
                name="hrms_audit_compliance",
                description="Audit employee records or attendance logs for statutory labor violations.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "tenant_id": {"type": "string", "default": "TENANT-01"},
                        "jurisdiction": {"type": "string", "enum": ["IN", "AE", "SA", "US"], "default": "AE"},
                        "employees": {"type": "array", "items": {"type": "object"}},
                    },
                    "required": ["jurisdiction", "employees"],
                },
            ),
            McpToolDefinition(
                name="hrms_score_candidate_ats",
                description="Score candidate resume against job requisitions with explainable rubric.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "resume_text": {"type": "string"},
                        "required_skills": {"type": "array", "items": {"type": "string"}},
                        "required_experience_years": {"type": "number", "default": 3.0},
                        "candidate_experience_years": {"type": "number", "default": 3.0},
                    },
                    "required": ["resume_text", "required_skills"],
                },
            ),
            McpToolDefinition(
                name="hrms_audit_job_description",
                description="Audit job descriptions for exclusionary/biased language and EEO compliance.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "jd_text": {"type": "string", "description": "Full job description text"},
                    },
                    "required": ["jd_text"],
                },
            ),
            McpToolDefinition(
                name="hrms_evaluate_leave_request",
                description="Evaluate employee leave requests against allocated balances and statutory rules.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "employee_id": {"type": "string"},
                        "leave_type": {"type": "string", "default": "ANNUAL"},
                        "requested_days": {"type": "number", "default": 2.0},
                        "current_balance": {"type": "number", "default": 10.0},
                    },
                    "required": ["employee_id", "requested_days"],
                },
            ),
            McpToolDefinition(
                name="hrms_detect_impossible_travel",
                description="Evaluate two consecutive GPS attendance punches for impossible travel velocity.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "punch1": {
                            "type": "object",
                            "properties": {
                                "lat": {"type": "number"},
                                "lng": {"type": "number"},
                                "timestamp": {"type": "string"},
                            },
                        },
                        "punch2": {
                            "type": "object",
                            "properties": {
                                "lat": {"type": "number"},
                                "lng": {"type": "number"},
                                "timestamp": {"type": "string"},
                            },
                        },
                    },
                    "required": ["punch1", "punch2"],
                },
            ),
        ]

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute MCP tool invocation."""
        if name == "hrms_query_labor_statutes":
            query = arguments.get("query", "")
            jur = arguments.get("jurisdiction", "IN")
            citations = self.kb.search(query=query, jurisdiction=jur)
            return {
                "results": [
                    {
                        "act": c.act_name,
                        "section": c.section_or_article,
                        "title": c.title,
                        "summary": c.summary,
                        "citation": self.kb.format_citation(c),
                    }
                    for c in citations
                ]
            }

        elif name == "hrms_calculate_payroll_ctc":
            ctc = float(arguments.get("annual_ctc", 0.0))
            jur = arguments.get("jurisdiction", "IN")
            if jur == "AE":
                res = self.payroll.structure_uae_salary(monthly_gross=ctc / 12.0)
            else:
                res = self.payroll.structure_india_salary(annual_ctc=ctc)
            return res.model_dump()

        elif name == "hrms_calculate_uae_eosb":
            basic = float(arguments.get("basic_wage_monthly", 0.0))
            tenure = float(arguments.get("tenure_years", 0.0))
            res = self.payroll.calculate_uae_eosb(basic_wage_monthly=basic, tenure_years=tenure)
            return res.model_dump()

        elif name == "hrms_audit_compliance":
            tenant_id = arguments.get("tenant_id", "TENANT-01")
            jur = arguments.get("jurisdiction", "AE")
            emps = arguments.get("employees", [])
            report = self.compliance.audit_employees(tenant_id=tenant_id, jurisdiction=jur, employees=emps)
            return report.model_dump()

        elif name == "hrms_score_candidate_ats":
            res = self.recruitment.score_candidate(
                resume_text=arguments.get("resume_text", ""),
                required_skills=arguments.get("required_skills", []),
                required_experience_years=float(arguments.get("required_experience_years", 3.0)),
                candidate_experience_years=float(arguments.get("candidate_experience_years", 3.0)),
            )
            return res.model_dump()

        elif name == "hrms_audit_job_description":
            res = self.recruitment.audit_job_description_bias(arguments.get("jd_text", ""))
            return res.model_dump()

        elif name == "hrms_evaluate_leave_request":
            res = self.leave.evaluate_leave_request(
                employee_id=arguments.get("employee_id", "EMP-001"),
                leave_type=arguments.get("leave_type", "ANNUAL"),
                requested_days=float(arguments.get("requested_days", 1.0)),
                current_balance=float(arguments.get("current_balance", 10.0)),
            )
            return res.model_dump()

        elif name == "hrms_detect_impossible_travel":
            res = self.leave.detect_impossible_travel(
                punch1=arguments.get("punch1", {}),
                punch2=arguments.get("punch2", {}),
            )
            return res.model_dump()

        else:
            raise ValueError(f"Unknown MCP tool: {name}")
