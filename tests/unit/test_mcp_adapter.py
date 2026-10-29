"""P11: the MCP server is a thin adapter over the validated tool registry.

Tenant and permissions come from the AuthContext the server is built with, never from tool arguments; arguments get
the same schema validation as the chat agent; calculators that need user-stated numbers are not exposed, because an
MCP client is itself a model and there is no user turn to ground its numbers against (No-LLM-Numbers gate).
"""

import asyncio

import pytest

from hrms_plugin.agents.tools import USER_STATED_PARAMS, AgenticToolRegistry
from hrms_plugin.security.rbac import AuthContext, UserRole
from hrms_plugin.server.mcp_server import HrmsMcpServer

ADMIN = AuthContext(user_id="u", tenant_id="acme", roles=frozenset({UserRole.SUPER_ADMIN}))
STAFF = AuthContext(user_id="s", tenant_id="acme", roles=frozenset({UserRole.EMPLOYEE}))


def call(server, name, args):
    return asyncio.run(server.call_tool(name, args))


def test_lists_only_permitted_tools_without_user_stated_numbers():
    names = {t.name for t in HrmsMcpServer(ADMIN).list_tools()}
    assert "hrms_search_labor_regulations" in names and "hrms_calculate_pf_contribution" in names
    assert not names & {f"hrms_{n}" for n in USER_STATED_PARAMS}
    staff = {t.name for t in HrmsMcpServer(STAFF).list_tools()}
    assert staff < names and "hrms_calculate_pf_contribution" not in staff


def test_server_owned_parameters_are_hidden_from_the_schema():
    for tool in HrmsMcpServer(ADMIN).list_tools():
        assert "tenant_id" not in tool.input_schema.get("properties", {})
        assert "jurisdiction" not in tool.input_schema.get("properties", {})
        assert "tenant_id" not in tool.input_schema.get("required", [])


def test_valid_call_runs_through_the_registry():
    out = call(HrmsMcpServer(ADMIN, jurisdiction="AE"), "hrms_search_labor_regulations", {"query": "gratuity"})
    assert "error" not in out


@pytest.mark.parametrize("args", [{"query": "x", "tenant_id": "globex"}, {"query": "x", "jurisdiction": "IN"}])
def test_tenant_and_jurisdiction_can_never_come_from_arguments(args):
    out = call(HrmsMcpServer(ADMIN, jurisdiction="AE"), "hrms_search_labor_regulations", args)
    assert out["error"] == "SERVER_OWNED_ARGUMENT"


def test_tenant_comes_from_auth(monkeypatch):
    seen = {}

    async def spy(self, tenant_id, employee_id, period, jurisdiction):
        seen.update(tenant=tenant_id, jurisdiction=jurisdiction)
        return {"ok": True}

    from hrms_plugin.payroll.service import PayrollService

    monkeypatch.setattr(PayrollService, "calculate_pf", spy)
    call(HrmsMcpServer(ADMIN, jurisdiction="IN"), "hrms_calculate_pf_contribution",
         {"employee_id": "E1", "period": "2026-09"})
    assert seen == {"tenant": "acme", "jurisdiction": "IN"}


@pytest.mark.parametrize("name, args, error", [
    ("hrms_search_labor_regulations", {}, "INVALID_ARGUMENTS"),
    ("hrms_search_labor_regulations", {"query": 5}, "INVALID_ARGUMENTS"),
    ("hrms_calculate_eosb_gratuity", {"basic_wage": 5000, "tenure_years": 6}, "TOOL_NOT_AVAILABLE"),
    ("hrms_drop_database", {}, "TOOL_NOT_AVAILABLE"),
    ("search_labor_regulations", {"query": "x"}, "TOOL_NOT_AVAILABLE"),
])
def test_invalid_or_unexposed_calls_are_refused(name, args, error):
    assert call(HrmsMcpServer(ADMIN, jurisdiction="AE"), name, args)["error"] == error


def test_permission_is_enforced_at_call_time():
    out = call(HrmsMcpServer(STAFF, jurisdiction="IN"), "hrms_calculate_pf_contribution",
               {"employee_id": "E1", "period": "2026-09"})
    assert out["error"] == "TOOL_NOT_AVAILABLE"


def test_jurisdiction_must_be_valid():
    with pytest.raises(ValueError):
        HrmsMcpServer(ADMIN, jurisdiction="XX")


def test_shares_a_registry_when_given_one():
    registry = AgenticToolRegistry()
    assert HrmsMcpServer(ADMIN, registry=registry).registry is registry
