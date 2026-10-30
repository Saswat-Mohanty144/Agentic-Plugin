"""Unit and integration tests for Task F (Decision D8: MCP Streamable HTTP transport).

Protocol: MCP Streamable HTTP with JSON responses only, JSON-RPC 2.0.
Routes:
  - POST /v1/mcp
  - GET /v1/mcp -> 405 with Allow: POST
"""

import json
from typing import Any, Dict

import pytest

from hrms_plugin.security.rbac import Permission

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _rpc_req(method: str, params: Any = None, req_id: Any = "1") -> Dict[str, Any]:
    req: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        req["params"] = params
    if req_id is not None:
        req["id"] = req_id
    return req


def _headers(bearer_headers: Dict[str, str], jurisdiction: str = "IN", origin: str = None) -> Dict[str, str]:
    h = dict(bearer_headers)
    if jurisdiction is not None:
        h["X-HRMS-Jurisdiction"] = jurisdiction
    if origin is not None:
        h["Origin"] = origin
    return h


# -----------------------------------------------------------------------------
# Route & Method Tests
# -----------------------------------------------------------------------------

def test_get_mcp_returns_405_with_allow_post(make_client):
    """GET /v1/mcp is rejected with 405 Method Not Allowed and Allow: POST."""
    client = make_client()
    res = client.get("/v1/mcp")
    assert res.status_code == 405
    assert res.headers.get("allow") == "POST"


def test_get_mcp_with_auth_and_headers_still_405(make_client, bearer):
    """GET /v1/mcp even with credentials returns 405 with Allow: POST."""
    client = make_client()
    res = client.get("/v1/mcp", headers=_headers(bearer(roles=["staff"])))
    assert res.status_code == 405
    assert res.headers.get("allow") == "POST"


# -----------------------------------------------------------------------------
# Security: Auth & RBAC
# -----------------------------------------------------------------------------

def test_post_mcp_without_token_returns_401(make_client):
    """Calling POST /v1/mcp without Authorization token returns 401."""
    client = make_client()
    res = client.post("/v1/mcp", json=_rpc_req("ping"), headers={"X-HRMS-Jurisdiction": "IN"})
    assert res.status_code == 401
    assert res.json().get("error") == "UNAUTHENTICATED"


def test_post_mcp_lacking_permission_returns_403(make_client, bearer):
    """Calling POST /v1/mcp with role lacking USE_COPILOT returns 403."""
    client = make_client()
    # "janitor" role has no USE_COPILOT permission in the test fixture
    res = client.post(
        "/v1/mcp",
        json=_rpc_req("ping"),
        headers=_headers(bearer(roles=["janitor"]), jurisdiction="IN"),
    )
    assert res.status_code == 403
    assert res.json().get("error") == "FORBIDDEN"
    assert res.json().get("permission") == Permission.USE_COPILOT.value


def test_post_mcp_holding_permission_passes_auth(make_client, bearer):
    """Calling POST /v1/mcp with role holding USE_COPILOT passes auth."""
    client = make_client()
    res = client.post(
        "/v1/mcp",
        json=_rpc_req("ping"),
        headers=_headers(bearer(roles=["staff"]), jurisdiction="IN"),
    )
    assert res.status_code == 200


# -----------------------------------------------------------------------------
# Security: Jurisdiction Header
# -----------------------------------------------------------------------------

def test_post_mcp_missing_jurisdiction_header_returns_400(make_client, bearer):
    """Missing X-HRMS-Jurisdiction header returns 400 JURISDICTION_REQUIRED."""
    client = make_client()
    res = client.post("/v1/mcp", json=_rpc_req("ping"), headers=bearer(roles=["staff"]))
    assert res.status_code == 400
    assert res.json() == {"error": "JURISDICTION_REQUIRED"}


@pytest.mark.parametrize("invalid_jur", ["", "XX", "in", "ae", "sa", "us", "IND", "UAE", "global"])
def test_post_mcp_invalid_jurisdiction_returns_400(make_client, bearer, invalid_jur):
    """Jurisdiction not in (IN, AE, SA, US) returns 400 JURISDICTION_REQUIRED."""
    client = make_client()
    res = client.post(
        "/v1/mcp",
        json=_rpc_req("ping"),
        headers={"Authorization": bearer(roles=["staff"])["Authorization"], "X-HRMS-Jurisdiction": invalid_jur},
    )
    assert res.status_code == 400
    assert res.json() == {"error": "JURISDICTION_REQUIRED"}


@pytest.mark.parametrize("valid_jur", ["IN", "AE", "SA", "US"])
def test_post_mcp_valid_jurisdictions_accepted(make_client, bearer, valid_jur):
    """Every supported statutory jurisdiction (IN, AE, SA, US) is accepted."""
    client = make_client()
    res = client.post(
        "/v1/mcp",
        json=_rpc_req("ping"),
        headers=_headers(bearer(roles=["staff"]), jurisdiction=valid_jur),
    )
    assert res.status_code == 200
    assert res.json() == {"jsonrpc": "2.0", "id": "1", "result": {}}


# -----------------------------------------------------------------------------
# Security: Origin Header
# -----------------------------------------------------------------------------

def test_post_mcp_disallowed_origin_returns_403(make_client, bearer):
    """Origin present but not in allowed origins returns 403 ORIGIN_NOT_ALLOWED."""
    client = make_client()
    res = client.post(
        "/v1/mcp",
        json=_rpc_req("ping"),
        headers=_headers(bearer(roles=["staff"]), jurisdiction="IN", origin="https://evil.test"),
    )
    assert res.status_code == 403
    assert res.json() == {"error": "ORIGIN_NOT_ALLOWED"}


def test_post_mcp_allowed_origin_succeeds(make_client, bearer):
    """Origin present and in allowed origins succeeds."""
    client = make_client()
    res = client.post(
        "/v1/mcp",
        json=_rpc_req("ping"),
        headers=_headers(bearer(roles=["staff"]), jurisdiction="IN", origin="https://hr.acme.test"),
    )
    assert res.status_code == 200
    assert res.json() == {"jsonrpc": "2.0", "id": "1", "result": {}}


def test_post_mcp_no_origin_header_succeeds(make_client, bearer):
    """Origin header is optional (e.g. for non-browser desktop / CLI clients)."""
    client = make_client()
    res = client.post(
        "/v1/mcp",
        json=_rpc_req("ping"),
        headers=_headers(bearer(roles=["staff"]), jurisdiction="IN"),
    )
    assert res.status_code == 200


# -----------------------------------------------------------------------------
# Security: Rate Limiting
# -----------------------------------------------------------------------------

def test_post_mcp_rate_limiting(make_client, bearer, monkeypatch):
    """Rate limit exhaustion on POST /v1/mcp returns 429 RATE_LIMITED with Retry-After."""
    client = make_client()
    # Force the rate limiter to report retry after 5.2 seconds
    monkeypatch.setattr(client.app.state.rate_limiter, "check", lambda tenant_id: 5.2)

    res = client.post(
        "/v1/mcp",
        json=_rpc_req("ping"),
        headers=_headers(bearer(roles=["staff"]), jurisdiction="IN"),
    )
    assert res.status_code == 429
    assert res.json() == {"error": "RATE_LIMITED"}
    assert res.headers.get("retry-after") == "6"


# -----------------------------------------------------------------------------
# JSON-RPC Protocol Errors (-32700, -32600, -32601, -32602)
# -----------------------------------------------------------------------------

def test_post_mcp_bad_json_returns_32700(make_client, bearer):
    """Malformed JSON payload returns JSON-RPC -32700 Parse error (HTTP 200)."""
    client = make_client()
    res = client.post(
        "/v1/mcp",
        content="{invalid json string",
        headers={**_headers(bearer(roles=["staff"]), jurisdiction="IN"), "Content-Type": "application/json"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["jsonrpc"] == "2.0"
    assert data["id"] is None
    assert data["error"]["code"] == -32700
    assert "Parse error" in data["error"]["message"]


def test_post_mcp_json_array_batch_returns_32600(make_client, bearer):
    """JSON-RPC array batch is rejected with -32600 Invalid Request."""
    client = make_client()
    res = client.post(
        "/v1/mcp",
        json=[_rpc_req("ping", req_id="1"), _rpc_req("ping", req_id="2")],
        headers=_headers(bearer(roles=["staff"]), jurisdiction="IN"),
    )
    assert res.status_code == 200
    data = res.json()
    assert data["jsonrpc"] == "2.0"
    assert data["id"] is None
    assert data["error"]["code"] == -32600
    assert "Invalid Request" in data["error"]["message"]


@pytest.mark.parametrize("payload", [123, "string", True])
def test_post_mcp_non_object_body_returns_32600(make_client, bearer, payload):
    """Non-object JSON bodies return -32600 Invalid Request."""
    client = make_client()
    res = client.post(
        "/v1/mcp",
        json=payload,
        headers=_headers(bearer(roles=["staff"]), jurisdiction="IN"),
    )
    assert res.status_code == 200
    data = res.json()
    assert data["jsonrpc"] == "2.0"
    assert data["id"] is None
    assert data["error"]["code"] == -32600


@pytest.mark.parametrize("bad_jsonrpc", [None, "1.0", "2", 2.0, True])
def test_post_mcp_invalid_jsonrpc_version_returns_32600(make_client, bearer, bad_jsonrpc):
    """Missing or non-'2.0' jsonrpc member returns -32600."""
    client = make_client()
    body = {"method": "ping", "id": 1}
    if bad_jsonrpc is not None:
        body["jsonrpc"] = bad_jsonrpc
    res = client.post(
        "/v1/mcp",
        json=body,
        headers=_headers(bearer(roles=["staff"]), jurisdiction="IN"),
    )
    assert res.status_code == 200
    data = res.json()
    assert data["jsonrpc"] == "2.0"
    assert data["error"]["code"] == -32600


@pytest.mark.parametrize("bad_method", [None, 123, [], {}, True])
def test_post_mcp_invalid_method_member_returns_32600(make_client, bearer, bad_method):
    """Missing or non-string method member returns -32600."""
    client = make_client()
    body = {"jsonrpc": "2.0", "id": "1"}
    if bad_method is not None:
        body["method"] = bad_method
    res = client.post(
        "/v1/mcp",
        json=body,
        headers=_headers(bearer(roles=["staff"]), jurisdiction="IN"),
    )
    assert res.status_code == 200
    data = res.json()
    assert data["jsonrpc"] == "2.0"
    assert data["error"]["code"] == -32600


def test_post_mcp_unknown_method_returns_32601(make_client, bearer):
    """Unknown method returns JSON-RPC -32601 Method not found."""
    client = make_client()
    res = client.post(
        "/v1/mcp",
        json=_rpc_req("non_existent_method", req_id="xyz"),
        headers=_headers(bearer(roles=["staff"]), jurisdiction="IN"),
    )
    assert res.status_code == 200
    data = res.json()
    assert data["jsonrpc"] == "2.0"
    assert data["id"] == "xyz"
    assert data["error"]["code"] == -32601
    assert "Method not found" in data["error"]["message"]


@pytest.mark.parametrize("bad_params", ["string", 123, []])
def test_post_mcp_tools_call_invalid_params_type_returns_32602(make_client, bearer, bad_params):
    """tools/call with non-dict params returns -32602 Invalid params."""
    client = make_client()
    res = client.post(
        "/v1/mcp",
        json=_rpc_req("tools/call", params=bad_params, req_id="tc1"),
        headers=_headers(bearer(roles=["staff"]), jurisdiction="IN"),
    )
    assert res.status_code == 200
    data = res.json()
    assert data["id"] == "tc1"
    assert data["error"]["code"] == -32602


@pytest.mark.parametrize("bad_call_params", [
    {},  # missing name
    {"arguments": {}},  # missing name
    {"name": 123, "arguments": {}},  # name not string
    {"name": "hrms_search", "arguments": "not-a-dict"},  # arguments not dict
])
def test_post_mcp_tools_call_missing_or_invalid_fields_returns_32602(make_client, bearer, bad_call_params):
    """tools/call with missing name or invalid argument types returns -32602."""
    client = make_client()
    res = client.post(
        "/v1/mcp",
        json=_rpc_req("tools/call", params=bad_call_params, req_id="tc2"),
        headers=_headers(bearer(roles=["staff"]), jurisdiction="IN"),
    )
    assert res.status_code == 200
    data = res.json()
    assert data["id"] == "tc2"
    assert data["error"]["code"] == -32602


# -----------------------------------------------------------------------------
# Methods: initialize
# -----------------------------------------------------------------------------

@pytest.mark.parametrize("client_version,expected_version", [
    ("2025-06-18", "2025-06-18"),
    ("2025-03-26", "2025-03-26"),
    ("2024-11-05", "2025-06-18"),
    ("draft", "2025-06-18"),
    (None, "2025-06-18"),
])
def test_initialize_negotiates_protocol_version(make_client, bearer, client_version, expected_version):
    """initialize negotiates protocolVersion (client requested if 2025-06-18 or 2025-03-26 else 2025-06-18)."""
    client = make_client()
    params = {"protocolVersion": client_version} if client_version else {}
    res = client.post(
        "/v1/mcp",
        json=_rpc_req("initialize", params=params, req_id="init-1"),
        headers=_headers(bearer(roles=["staff"]), jurisdiction="IN"),
    )
    assert res.status_code == 200
    data = res.json()
    assert data["jsonrpc"] == "2.0"
    assert data["id"] == "init-1"
    result = data["result"]
    assert result["protocolVersion"] == expected_version
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"] == {"name": "hrms-agentic-plugin", "version": "2.0.0"}


# -----------------------------------------------------------------------------
# Methods: notifications
# -----------------------------------------------------------------------------

def test_notifications_initialized_returns_202_empty_body(make_client, bearer):
    """notifications/initialized returns HTTP 202 with an empty body."""
    client = make_client()
    payload = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    res = client.post(
        "/v1/mcp",
        json=payload,
        headers=_headers(bearer(roles=["staff"]), jurisdiction="IN"),
    )
    assert res.status_code == 202
    assert res.content == b""


def test_any_notification_without_id_returns_202_empty_body(make_client, bearer):
    """Any notification request (no id) returns HTTP 202 with an empty body."""
    client = make_client()
    payload = {"jsonrpc": "2.0", "method": "custom/notification", "params": {"data": 123}}
    res = client.post(
        "/v1/mcp",
        json=payload,
        headers=_headers(bearer(roles=["staff"]), jurisdiction="IN"),
    )
    assert res.status_code == 202
    assert res.content == b""


# -----------------------------------------------------------------------------
# Methods: ping
# -----------------------------------------------------------------------------

def test_ping_returns_empty_result(make_client, bearer):
    """ping returns empty result dict."""
    client = make_client()
    res = client.post(
        "/v1/mcp",
        json=_rpc_req("ping", req_id="ping-99"),
        headers=_headers(bearer(roles=["staff"]), jurisdiction="IN"),
    )
    assert res.status_code == 200
    assert res.json() == {"jsonrpc": "2.0", "id": "ping-99", "result": {}}


# -----------------------------------------------------------------------------
# Methods: tools/list
# -----------------------------------------------------------------------------

def test_tools_list_returns_tools_with_input_schema(make_client, bearer):
    """tools/list returns {"tools": [{"name", "description", "inputSchema"}]} from HrmsMcpServer."""
    client = make_client()
    res = client.post(
        "/v1/mcp",
        json=_rpc_req("tools/list", req_id="tl-1"),
        headers=_headers(bearer(roles=["platform_admin"]), jurisdiction="IN"),
    )
    assert res.status_code == 200
    data = res.json()
    assert data["jsonrpc"] == "2.0"
    assert data["id"] == "tl-1"
    tools = data["result"]["tools"]
    assert isinstance(tools, list) and len(tools) > 0

    tool_names = {t["name"] for t in tools}
    assert "hrms_search_labor_regulations" in tool_names
    assert "hrms_calculate_pf_contribution" in tool_names

    # Check structure: name, description, inputSchema (camelCase)
    for tool in tools:
        assert "name" in tool
        assert "description" in tool
        assert "inputSchema" in tool
        assert "input_schema" not in tool  # Must be camelCase
        # Server owned params must not be visible in properties or required
        schema = tool["inputSchema"]
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        assert "tenant_id" not in properties
        assert "jurisdiction" not in properties
        assert "tenant_id" not in required
        assert "jurisdiction" not in required


def test_tools_list_filters_by_caller_permission(make_client, bearer):
    """tools/list exposes only tools permitted for the caller's AuthContext."""
    client = make_client()
    admin_res = client.post(
        "/v1/mcp",
        json=_rpc_req("tools/list", req_id="tl-admin"),
        headers=_headers(bearer(roles=["platform_admin"]), jurisdiction="IN"),
    )
    staff_res = client.post(
        "/v1/mcp",
        json=_rpc_req("tools/list", req_id="tl-staff"),
        headers=_headers(bearer(roles=["staff"]), jurisdiction="IN"),
    )
    admin_tools = {t["name"] for t in admin_res.json()["result"]["tools"]}
    staff_tools = {t["name"] for t in staff_res.json()["result"]["tools"]}

    assert "hrms_calculate_pf_contribution" in admin_tools
    assert "hrms_calculate_pf_contribution" not in staff_tools
    assert staff_tools < admin_tools


# -----------------------------------------------------------------------------
# Methods: tools/call
# -----------------------------------------------------------------------------

def test_tools_call_success(make_client, bearer):
    """tools/call executes tool and formats content, structuredContent, and isError."""
    client = make_client()
    res = client.post(
        "/v1/mcp",
        json=_rpc_req(
            "tools/call",
            params={"name": "hrms_search_labor_regulations", "arguments": {"query": "gratuity"}},
            req_id="call-1",
        ),
        headers=_headers(bearer(roles=["staff"]), jurisdiction="AE"),
    )
    assert res.status_code == 200
    data = res.json()
    assert data["jsonrpc"] == "2.0"
    assert data["id"] == "call-1"
    result = data["result"]
    assert result["isError"] is False
    assert "error" not in result["structuredContent"]
    assert result["structuredContent"]["results_count"] > 0
    assert result["content"] == [{"type": "text", "text": json.dumps(result["structuredContent"])}]


def test_tools_call_unexposed_tool_returns_is_error(make_client, bearer):
    """tools/call on unexposed/calculators returns result with isError=True."""
    client = make_client()
    res = client.post(
        "/v1/mcp",
        json=_rpc_req(
            "tools/call",
            params={"name": "hrms_calculate_eosb_gratuity", "arguments": {"basic_wage": 5000, "tenure_years": 6}},
            req_id="call-unexp",
        ),
        headers=_headers(bearer(roles=["platform_admin"]), jurisdiction="AE"),
    )
    assert res.status_code == 200
    data = res.json()
    assert data["id"] == "call-unexp"
    result = data["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["error"] == "TOOL_NOT_AVAILABLE"


def test_tools_call_server_owned_argument_rejected_with_is_error(make_client, bearer):
    """Passing server owned arguments in arguments returns isError=True."""
    client = make_client()
    res = client.post(
        "/v1/mcp",
        json=_rpc_req(
            "tools/call",
            params={"name": "hrms_search_labor_regulations", "arguments": {"query": "gratuity", "tenant_id": "other"}},
            req_id="call-owned",
        ),
        headers=_headers(bearer(roles=["staff"]), jurisdiction="AE"),
    )
    assert res.status_code == 200
    result = res.json()["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["error"] == "SERVER_OWNED_ARGUMENT"


def test_tools_call_invalid_arguments_returns_is_error(make_client, bearer):
    """Invalid tool arguments produce error in tool execution -> isError=True."""
    client = make_client()
    res = client.post(
        "/v1/mcp",
        json=_rpc_req(
            "tools/call",
            params={"name": "hrms_search_labor_regulations", "arguments": {"query": 12345}},
            req_id="call-bad-arg",
        ),
        headers=_headers(bearer(roles=["staff"]), jurisdiction="AE"),
    )
    assert res.status_code == 200
    result = res.json()["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["error"] == "INVALID_ARGUMENTS"


def test_tools_call_uses_token_tenant_and_header_jurisdiction(make_client, bearer, monkeypatch):
    """Tenant comes strictly from token and jurisdiction from X-HRMS-Jurisdiction header."""
    seen = {}

    async def spy(self, tenant_id, employee_id, period, jurisdiction):
        seen.update(tenant=tenant_id, jurisdiction=jurisdiction, employee_id=employee_id, period=period)
        return {"success": True, "amount": 1800}

    from hrms_plugin.payroll.service import PayrollService
    monkeypatch.setattr(PayrollService, "calculate_pf", spy)

    client = make_client()
    res = client.post(
        "/v1/mcp",
        json=_rpc_req(
            "tools/call",
            params={
                "name": "hrms_calculate_pf_contribution",
                "arguments": {"employee_id": "EMP-9", "period": "2026-09"},
            },
            req_id="call-spy",
        ),
        headers=_headers(bearer(tenant="acme", roles=["platform_admin"]), jurisdiction="IN"),
    )
    assert res.status_code == 200
    result = res.json()["result"]
    assert result["isError"] is False
    assert seen == {"tenant": "acme", "jurisdiction": "IN", "employee_id": "EMP-9", "period": "2026-09"}


# -----------------------------------------------------------------------------
# Boundary Cases
# -----------------------------------------------------------------------------

@pytest.mark.parametrize("req_id", [0, 42, "req-string-id", "uuid-1234-5678", None])
def test_json_rpc_id_types_preserved(make_client, bearer, req_id):
    """JSON-RPC id can be integer, string, or null, and is echoed back in the response."""
    client = make_client()
    res = client.post(
        "/v1/mcp",
        json={"jsonrpc": "2.0", "id": req_id, "method": "ping"},
        headers=_headers(bearer(roles=["staff"]), jurisdiction="IN"),
    )
    assert res.status_code == 200
    assert res.json()["id"] == req_id


def test_tools_call_defaults_empty_arguments_if_omitted(make_client, bearer):
    """tools/call with arguments omitted passes {} to the server."""
    client = make_client()
    res = client.post(
        "/v1/mcp",
        json=_rpc_req("tools/call", params={"name": "hrms_drop_database"}, req_id="c-noargs"),
        headers=_headers(bearer(roles=["platform_admin"]), jurisdiction="IN"),
    )
    assert res.status_code == 200
    result = res.json()["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["error"] == "TOOL_NOT_AVAILABLE"


# -----------------------------------------------------------------------------
# Follow-up Review Findings: Decimal and Non-JSON types in tools/call
# -----------------------------------------------------------------------------

def test_tools_call_with_decimal_and_non_json_types_returns_200(make_client, bearer, monkeypatch):
    """tools/call serializes Decimal, datetime and other non-JSON types into strings without 500 error."""
    from datetime import datetime
    from decimal import Decimal

    from hrms_plugin.server.mcp_server import HrmsMcpServer

    async def mock_call_tool(self, name, arguments):
        return {
            "success": True,
            "wage": Decimal("1234.50"),
            "calculated_at": datetime(2026, 10, 30, 12, 0, 0),
            "nested": {"bonus": Decimal("500.00")},
        }

    monkeypatch.setattr(HrmsMcpServer, "call_tool", mock_call_tool)

    client = make_client()
    res = client.post(
        "/v1/mcp",
        json=_rpc_req(
            "tools/call",
            params={"name": "hrms_calculate_pf_contribution", "arguments": {"employee_id": "EMP-1"}},
            req_id="call-decimal",
        ),
        headers=_headers(bearer(roles=["platform_admin"]), jurisdiction="IN"),
    )
    assert res.status_code == 200
    data = res.json()
    assert data["id"] == "call-decimal"
    result = data["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["wage"] == "1234.50"
    assert result["structuredContent"]["calculated_at"] == "2026-10-30 12:00:00"
    assert result["structuredContent"]["nested"]["bonus"] == "500.00"
    content_text = json.loads(result["content"][0]["text"])
    assert content_text["wage"] == "1234.50"
    assert content_text["nested"]["bonus"] == "500.00"


def test_tools_call_with_decimal_in_list_of_objects(make_client, bearer, monkeypatch):
    """tools/call safely serializes Decimal objects inside lists."""
    from decimal import Decimal

    from hrms_plugin.server.mcp_server import HrmsMcpServer

    async def mock_call_tool(self, name, arguments):
        return {
            "success": True,
            "items": [{"code": "BASE", "amount": Decimal("2500.75")}],
        }

    monkeypatch.setattr(HrmsMcpServer, "call_tool", mock_call_tool)

    client = make_client()
    res = client.post(
        "/v1/mcp",
        json=_rpc_req(
            "tools/call",
            params={"name": "hrms_calculate_pf_contribution", "arguments": {"employee_id": "EMP-1"}},
            req_id="call-list-decimal",
        ),
        headers=_headers(bearer(roles=["platform_admin"]), jurisdiction="IN"),
    )
    assert res.status_code == 200
    result = res.json()["result"]
    assert result["structuredContent"]["items"][0]["amount"] == "2500.75"


# -----------------------------------------------------------------------------
# Follow-up Review Findings: MCP-Protocol-Version Request Header
# -----------------------------------------------------------------------------

@pytest.mark.parametrize("version", ["2024-11-05", "1.0.0", "unsupported", "2025-01-01", ""])
def test_post_mcp_unsupported_protocol_version_header_returns_400(make_client, bearer, version):
    """If MCP-Protocol-Version request header is present and unsupported, returns 400 UNSUPPORTED_PROTOCOL_VERSION."""
    client = make_client()
    headers = _headers(bearer(roles=["staff"]), jurisdiction="IN")
    headers["MCP-Protocol-Version"] = version
    res = client.post("/v1/mcp", json=_rpc_req("ping"), headers=headers)
    assert res.status_code == 400
    assert res.json() == {"error": "UNSUPPORTED_PROTOCOL_VERSION"}


@pytest.mark.parametrize("version", ["2025-06-18", "2025-03-26"])
def test_post_mcp_supported_protocol_version_header_accepted(make_client, bearer, version):
    """If MCP-Protocol-Version request header is present and supported, request succeeds."""
    client = make_client()
    headers = _headers(bearer(roles=["staff"]), jurisdiction="IN")
    headers["MCP-Protocol-Version"] = version
    res = client.post("/v1/mcp", json=_rpc_req("ping"), headers=headers)
    assert res.status_code == 200
    assert res.json()["result"] == {}


def test_post_mcp_protocol_version_header_case_insensitive(make_client, bearer):
    """MCP-Protocol-Version header matching is case-insensitive."""
    client = make_client()
    headers = _headers(bearer(roles=["staff"]), jurisdiction="IN")
    headers["mcp-protocol-version"] = "unsupported-ver"
    res = client.post("/v1/mcp", json=_rpc_req("ping"), headers=headers)
    assert res.status_code == 400
    assert res.json() == {"error": "UNSUPPORTED_PROTOCOL_VERSION"}


def test_post_mcp_absent_protocol_version_header_accepted(make_client, bearer):
    """If MCP-Protocol-Version request header is absent, request is accepted."""
    client = make_client()
    headers = _headers(bearer(roles=["staff"]), jurisdiction="IN")
    assert "MCP-Protocol-Version" not in headers
    res = client.post("/v1/mcp", json=_rpc_req("ping"), headers=headers)
    assert res.status_code == 200


# -----------------------------------------------------------------------------
# Follow-up Review Findings: Shared enforce_rate_limit Helper
# -----------------------------------------------------------------------------

def test_enforce_rate_limit_helper_imported_and_enforced():
    """enforce_rate_limit raises 429 RATE_LIMITED when limiter.check returns retry seconds."""
    from unittest.mock import MagicMock

    from hrms_plugin.governance.rate_limit import enforce_rate_limit
    from hrms_plugin.security.http_auth import ApiError
    from hrms_plugin.security.rbac import AuthContext, UserRole

    mock_request = MagicMock()
    mock_auth = AuthContext(
        tenant_id="tenant-x",
        user_id="user-1",
        roles=frozenset([UserRole.EMPLOYEE]),
    )

    # When check returns None: proceeds without error
    mock_request.app.state.rate_limiter.check.return_value = None
    enforce_rate_limit(mock_request, mock_auth)

    # When check returns 5.2 seconds: raises 429 with ceil(5.2) -> 6
    mock_request.app.state.rate_limiter.check.return_value = 5.2
    with pytest.raises(ApiError) as exc_info:
        enforce_rate_limit(mock_request, mock_auth)
    assert exc_info.value.status_code == 429
    assert exc_info.value.body == {"error": "RATE_LIMITED"}
    assert exc_info.value.headers == {"Retry-After": "6"}

    # When check returns 0.2 seconds: raises 429 with max(1, ceil(0.2)) -> 1
    mock_request.app.state.rate_limiter.check.return_value = 0.2
    with pytest.raises(ApiError) as exc_info:
        enforce_rate_limit(mock_request, mock_auth)
    assert exc_info.value.headers == {"Retry-After": "1"}


def test_app_and_mcp_share_enforce_rate_limit():
    """Both app.py and mcp_http.py use enforce_rate_limit from governance.rate_limit."""
    import hrms_plugin.server.app as app_mod
    import hrms_plugin.server.mcp_http as mcp_mod
    from hrms_plugin.governance.rate_limit import enforce_rate_limit

    app_func = getattr(app_mod, "enforce_rate_limit", getattr(app_mod, "_rate_limit", None))
    assert app_func is enforce_rate_limit
    assert getattr(mcp_mod, "enforce_rate_limit", None) is enforce_rate_limit
