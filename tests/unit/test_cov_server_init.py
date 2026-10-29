"""Coverage tests for hrms_plugin/server/__init__.py."""

from __future__ import annotations

import pytest
from fastapi import FastAPI

import hrms_plugin.server as server_pkg
from hrms_plugin.server.mcp_server import HrmsMcpServer


def test_lazy_export_app():
    """Positive test: Lazy attribute loader returns FastAPI instance."""
    app = server_pkg.__getattr__("app")
    assert isinstance(app, FastAPI)
    assert app.title == "Agentic HRMS AI Plugin Service"


def test_lazy_export_mcp_server():
    """Positive test: Lazy attribute loader returns MCP server class."""
    mcp_cls = server_pkg.__getattr__("HrmsMcpServer")
    assert mcp_cls is HrmsMcpServer
    assert mcp_cls.__name__ == "HrmsMcpServer"


def test_lazy_export_unknown_attribute_raises():
    """Negative test: Accessing undefined attribute raises AttributeError."""
    with pytest.raises(AttributeError) as excinfo:
        server_pkg.__getattr__("unknown_attribute_xyz")
    assert "unknown_attribute_xyz" in str(excinfo.value)


def test_package_all_exports():
    """Boundary test: __all__ strictly matches expected export symbols."""
    assert server_pkg.__all__ == ["app", "HrmsMcpServer"]
