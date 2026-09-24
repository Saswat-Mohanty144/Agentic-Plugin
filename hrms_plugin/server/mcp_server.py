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

        else:
            raise ValueError(f"Unknown MCP tool: {name}")
