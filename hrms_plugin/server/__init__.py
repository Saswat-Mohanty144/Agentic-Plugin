"""Server and MCP interface package for the Standalone HRMS Plugin.

Exports are lazy: importing `hrms_plugin.server.mcp_server` must not build the HTTP app,
which requires a tenant registry.
"""

from typing import Any

__all__ = ["app", "HrmsMcpServer"]


def __getattr__(name: str) -> Any:
    if name == "app":
        from hrms_plugin.server.app import app

        return app
    if name == "HrmsMcpServer":
        from hrms_plugin.server.mcp_server import HrmsMcpServer

        return HrmsMcpServer
    raise AttributeError(name)
