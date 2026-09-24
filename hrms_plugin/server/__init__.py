"""Server and MCP interface package for the Standalone HRMS Plugin."""

from hrms_plugin.server.app import app
from hrms_plugin.server.mcp_server import HrmsMcpServer

__all__ = ["app", "HrmsMcpServer"]
