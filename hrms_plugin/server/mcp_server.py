"""Model Context Protocol (MCP) adapter for the Agentic HRMS AI Plugin (P11).

A thin adapter over AgenticToolRegistry, so MCP gets exactly the chat agent's guarantees: schema-validated
arguments, permissions from the caller's AuthContext, and tenant and jurisdiction set by the server, never by tool
arguments. Calculators that need user-stated numbers are not exposed: an MCP client is itself a model, and there is
no user turn to ground its numbers against (No-LLM-Numbers gate).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from hrms_plugin.agents.tools import USER_STATED_PARAMS, AgenticToolRegistry
from hrms_plugin.ai.llm import ToolDefinition
from hrms_plugin.security.rbac import AuthContext

PREFIX = "hrms_"
SERVER_OWNED = ("tenant_id", "jurisdiction")
JURISDICTIONS = ("IN", "AE", "SA", "US")


class McpToolDefinition(BaseModel):
    name: str
    description: str
    input_schema: Dict[str, Any]


def _public_schema(parameters: Dict[str, Any]) -> Dict[str, Any]:
    """The tool schema without the parameters only the server may set."""
    properties = {k: v for k, v in parameters.get("properties", {}).items() if k not in SERVER_OWNED}
    required = [r for r in parameters.get("required", []) if r not in SERVER_OWNED]
    return {**parameters, "properties": properties, "required": required}


class HrmsMcpServer:
    """MCP tools for one authenticated caller in one jurisdiction."""

    def __init__(self, auth: AuthContext, jurisdiction: str = "IN", registry: Optional[AgenticToolRegistry] = None):
        if jurisdiction not in JURISDICTIONS:
            raise ValueError(f"jurisdiction must be one of {JURISDICTIONS}")
        self.auth = auth
        self.jurisdiction = jurisdiction
        self.registry = registry or AgenticToolRegistry()

    def _exposed(self) -> List[ToolDefinition]:
        return [d for d in self.registry.definitions_for(self.auth) if d.name not in USER_STATED_PARAMS]

    def list_tools(self) -> List[McpToolDefinition]:
        return [McpToolDefinition(name=PREFIX + d.name, description=d.description,
                                  input_schema=_public_schema(d.parameters)) for d in self._exposed()]

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        base = name[len(PREFIX):] if name.startswith(PREFIX) else None
        definition = next((d for d in self._exposed() if d.name == base), None)
        if definition is None:
            return {"success": False, "error": "TOOL_NOT_AVAILABLE", "tool": name}
        owned = sorted(set(arguments) & set(SERVER_OWNED))
        if owned:
            return {"success": False, "error": "SERVER_OWNED_ARGUMENT", "params": owned}
        properties = definition.parameters.get("properties", {})
        trusted = {"tenant_id": self.auth.tenant_id, "jurisdiction": self.jurisdiction}
        args = {**arguments, **{k: v for k, v in trusted.items() if k in properties}}
        return await self.registry.execute(definition.name, args)
