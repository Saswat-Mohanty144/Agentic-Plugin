"""Tests for gap v2: protocol seam (REST and MCP Streamable HTTP JSON-RPC transports)."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import httpx
import pytest

from hrms_plugin.integrations.declarative import (
    ConnectorError,
    DeclarativeConnector,
    ProfileError,
    load_profile,
    observed_shape,
    parse_profile,
)
from hrms_plugin.integrations.host_registry import HostConfig

try:
    from hrms_plugin.integrations.declarative import McpTransport, RestTransport
except ImportError:
    McpTransport = None  # type: ignore[assignment,misc]
    RestTransport = None  # type: ignore[assignment,misc]

ENV = {"TEST_BEARER_TOKEN": "super-secret-mcp-bearer-token"}


class FakeMcpServer:
    def __init__(
        self,
        tool_results: Optional[List[Dict[str, Any]]] = None,
        *,
        session_id: Optional[str] = "sess-12345",
        protocol_version: str = "2025-06-18",
        sse_framed: bool = False,
        init_status: int = 200,
        notify_status: int = 200,
        tool_status: int = 200,
        jsonrpc_error: Optional[Dict[str, Any]] = None,
        tool_is_error: bool = False,
        error_message: str = "HOST_INTERNAL_SENSITIVE_LEAK",
        bad_json: bool = False,
    ):
        self.tool_results = list(tool_results or [])
        self.session_id = session_id
        self.protocol_version = protocol_version
        self.sse_framed = sse_framed
        self.init_status = init_status
        self.notify_status = notify_status
        self.tool_status = tool_status
        self.jsonrpc_error = jsonrpc_error
        self.tool_is_error = tool_is_error
        self.error_message = error_message
        self.bad_json = bad_json
        self.requests: List[httpx.Request] = []
        self.call_count = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.bad_json:
            return httpx.Response(200, text="<!DOCTYPE html><html>not json</html>",
                                  headers={"content-type": "application/json"})

        try:
            body = json.loads(request.content.decode("utf-8")) if request.content else {}
        except Exception:
            body = {}

        method = body.get("method")
        req_id = body.get("id")

        if method == "initialize":
            if self.init_status != 200:
                return httpx.Response(self.init_status, text="init failed")
            headers = {"content-type": "text/event-stream" if self.sse_framed else "application/json"}
            if self.session_id:
                headers["mcp-session-id"] = self.session_id
            payload = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": self.protocol_version,
                    "capabilities": {},
                    "clientInfo": {"name": "fake-mcp-server", "version": "1.0.0"},
                },
            }
            if self.sse_framed:
                text = f": keep-alive\n\ndata: {json.dumps(payload)}\n\n"
                return httpx.Response(200, text=text, headers=headers)
            return httpx.Response(200, json=payload, headers=headers)

        if method == "notifications/initialized":
            if self.notify_status != 200:
                return httpx.Response(self.notify_status, text="notify failed")
            headers = {"content-type": "application/json"}
            if self.session_id:
                headers["mcp-session-id"] = self.session_id
            return httpx.Response(200, json={}, headers=headers)

        if method == "tools/call":
            self.call_count += 1
            if self.tool_status != 200:
                return httpx.Response(self.tool_status, text="tool failed")
            headers = {"content-type": "text/event-stream" if self.sse_framed else "application/json"}
            if self.session_id:
                headers["mcp-session-id"] = self.session_id

            if self.jsonrpc_error:
                payload = {"jsonrpc": "2.0", "id": req_id, "error": self.jsonrpc_error}
            elif self.tool_is_error:
                payload = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "isError": True,
                        "content": [{"type": "text", "text": self.error_message}],
                    },
                }
            else:
                tool_res = self.tool_results.pop(0) if self.tool_results else {"records": []}
                payload = {"jsonrpc": "2.0", "id": req_id, "result": tool_res}

            if self.sse_framed:
                text = f": comment\ndata: {json.dumps(payload)}\n\n"
                return httpx.Response(200, text=text, headers=headers)
            return httpx.Response(200, json=payload, headers=headers)

        return httpx.Response(404, text=f"unknown method: {method}")


def make_mcp_raw(
    entity_overrides: Optional[Dict[str, Any]] = None,
    profile_overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    ent: Dict[str, Any] = {
        "path": "/mcp",
        "tool": "list_requisitions",
        "arguments": {"tenant": "primary"},
        "items_at": "records",
        "id_from": "id",
        "fields": {
            "title": {"from": "title"},
            "department": {"from": "department"},
            "headcount": {"from": "headcount", "type": "int"},
            "status": {
                "from": "status",
                "map": {"OPEN": "OPEN", "CLOSED": "CLOSED", "PENDING": "PENDING"},
            },
            "description": {"from": "description"},
            "hiring_manager_ref": {"from": "manager"},
        },
    }
    if entity_overrides:
        ent.update(entity_overrides)
    prof: Dict[str, Any] = {
        "protocol": "mcp",
        "vendor": "iceipts",
        "auth": "bearer",
        "entities": {"REQUISITION": ent},
    }
    if profile_overrides:
        prof.update(profile_overrides)
    return prof


def make_mcp_profile(
    entity_overrides: Optional[Dict[str, Any]] = None,
    profile_overrides: Optional[Dict[str, Any]] = None,
):
    return parse_profile(make_mcp_raw(entity_overrides, profile_overrides))


def make_host(base_url: str = "https://mcp.internal") -> HostConfig:
    return HostConfig(
        tenant_id="tenant-mcp-1",
        vendor="iceipts",
        base_url=base_url,
        token_env="TEST_BEARER_TOKEN",
        service_user_id="11111111-2222-3333-4444-555555555555",
        timeout_seconds=10,
    )


def make_connector(server: FakeMcpServer, profile: Optional[Any] = None) -> DeclarativeConnector:
    prof = profile or make_mcp_profile()
    client = httpx.AsyncClient(transport=httpx.MockTransport(server.handler))
    return DeclarativeConnector(make_host(), prof, env=ENV, http_client=client)


def mcp_record(record_id: str = "REQ-101", **overrides) -> Dict[str, Any]:
    rec = {
        "id": record_id,
        "title": "Principal Engineer",
        "department": "Infrastructure",
        "headcount": 3,
        "status": "OPEN",
        "description": "Lead the platform team",
        "manager": "MGR-009",
    }
    rec.update(overrides)
    return rec


# 1. Profile parsing and validation tests --------------------------------------------------------------------------

def test_profile_protocol_defaults_to_rest():
    raw = make_mcp_raw()
    del raw["protocol"]
    # If protocol is rest, entity needs pagination and request_fields/id_from
    raw["entities"]["REQUISITION"]["pagination"] = {
        "kind": "offset", "start_param": "offset", "size_param": "limit", "size": 50
    }
    del raw["entities"]["REQUISITION"]["tool"]
    del raw["entities"]["REQUISITION"]["arguments"]
    prof = parse_profile(raw)
    assert prof.protocol == "rest"


def test_profile_protocol_explicit_rest():
    raw = make_mcp_raw({"pagination": {"kind": "offset", "start_param": "offset", "size_param": "limit", "size": 50}},
                       {"protocol": "rest"})
    del raw["entities"]["REQUISITION"]["tool"]
    del raw["entities"]["REQUISITION"]["arguments"]
    prof = parse_profile(raw)
    assert prof.protocol == "rest"


def test_frappe_and_iceipts_profiles_have_explicit_protocol_rest():
    frappe = load_profile("frappe")
    iceipts = load_profile("iceipts")
    assert frappe.protocol == "rest"
    assert iceipts.protocol == "rest"


def test_unknown_protocol_fails_profile_validation():
    raw = make_mcp_raw(profile_overrides={"protocol": "grpc"})
    with pytest.raises(ProfileError):
        parse_profile(raw)


def test_mcp_profile_bearer_auth_only():
    raw = make_mcp_raw(profile_overrides={"auth": "frappe_token"})
    with pytest.raises(ProfileError):
        parse_profile(raw)


def test_mcp_profile_requires_tool():
    raw = make_mcp_raw()
    del raw["entities"]["REQUISITION"]["tool"]
    with pytest.raises(ProfileError):
        parse_profile(raw)


def test_mcp_profile_cursor_paging_requires_both_or_neither():
    raw1 = make_mcp_raw(entity_overrides={"cursor_param": "cursor"})
    with pytest.raises(ProfileError):
        parse_profile(raw1)

    raw2 = make_mcp_raw(entity_overrides={"next_cursor_at": "pagination.next"})
    with pytest.raises(ProfileError):
        parse_profile(raw2)

    raw3 = make_mcp_raw(entity_overrides={"cursor_param": "cursor", "next_cursor_at": "pagination.next"})
    prof = parse_profile(raw3)
    ent = prof.entities["REQUISITION"]
    assert ent.cursor_param == "cursor"
    assert ent.next_cursor_at == "pagination.next"


def test_transports_exported():
    assert RestTransport is not None, "RestTransport must be exported"
    assert McpTransport is not None, "McpTransport must be exported"


# 2. MCP Transport and Connector happy paths ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mcp_happy_path_with_session_id():
    server = FakeMcpServer(
        tool_results=[{"structuredContent": {"records": [mcp_record("R-1"), mcp_record("R-2")]}}],
        session_id="session-xyz-789",
    )
    conn = make_connector(server)
    result = await conn.list_entity("REQUISITION")

    assert len(result.records) == 2
    assert result.records[0]["id"] == "R-1"
    assert result.records[0]["title"] == "Principal Engineer"
    assert result.records[0]["headcount"] == 3
    assert result.records[0]["status"] == "OPEN"
    assert result.records[1]["id"] == "R-2"
    assert len(result.rejected) == 0

    # Verify requests sent
    assert len(server.requests) == 3
    init_req, notify_req, tool_req = server.requests

    assert init_req.headers.get("authorization") == "Bearer super-secret-mcp-bearer-token"
    assert "application/json" in init_req.headers.get("accept", "")
    assert "text/event-stream" in init_req.headers.get("accept", "")
    init_body = json.loads(init_req.content.decode())
    assert init_body["method"] == "initialize"
    assert init_body["params"]["protocolVersion"] == "2025-06-18"
    assert init_body["params"]["clientInfo"]["name"] == "hrms-agentic-plugin"
    assert init_body["params"]["clientInfo"]["version"] == "2.0.0"

    assert notify_req.headers.get("mcp-session-id") == "session-xyz-789"
    assert notify_req.headers.get("mcp-protocol-version") == "2025-06-18"
    notify_body = json.loads(notify_req.content.decode())
    assert notify_body["method"] == "notifications/initialized"
    assert "id" not in notify_body

    assert tool_req.headers.get("mcp-session-id") == "session-xyz-789"
    assert tool_req.headers.get("mcp-protocol-version") == "2025-06-18"
    tool_body = json.loads(tool_req.content.decode())
    assert tool_body["method"] == "tools/call"
    assert tool_body["params"]["name"] == "list_requisitions"
    assert tool_body["params"]["arguments"] == {"tenant": "primary"}


@pytest.mark.asyncio
async def test_mcp_sse_framed_response():
    server = FakeMcpServer(
        tool_results=[{"structuredContent": {"records": [mcp_record("SSE-1")]}}],
        sse_framed=True,
    )
    conn = make_connector(server)
    result = await conn.list_entity("REQUISITION")

    assert len(result.records) == 1
    assert result.records[0]["id"] == "SSE-1"


@pytest.mark.asyncio
async def test_mcp_structured_content_vs_text_content():
    # 1. structuredContent
    s1 = FakeMcpServer(tool_results=[{"structuredContent": {"records": [mcp_record("SC-1")]}}])
    c1 = make_connector(s1)
    r1 = await c1.list_entity("REQUISITION")
    assert len(r1.records) == 1
    assert r1.records[0]["id"] == "SC-1"

    # 2. text content parsed from first text item when structuredContent is absent
    s2 = FakeMcpServer(tool_results=[{
        "content": [
            {"type": "text", "text": json.dumps({"records": [mcp_record("TXT-1")]})},
            {"type": "text", "text": "secondary comment"},
        ]
    }])
    c2 = make_connector(s2)
    r2 = await c2.list_entity("REQUISITION")
    assert len(r2.records) == 1
    assert r2.records[0]["id"] == "TXT-1"


@pytest.mark.asyncio
async def test_mcp_items_at_optional_when_result_is_list():
    raw = make_mcp_raw()
    del raw["entities"]["REQUISITION"]["items_at"]
    prof = parse_profile(raw)

    server = FakeMcpServer(tool_results=[{"structuredContent": [mcp_record("LIST-1")]}])
    conn = make_connector(server, profile=prof)
    result = await conn.list_entity("REQUISITION")
    assert len(result.records) == 1
    assert result.records[0]["id"] == "LIST-1"


# 3. Cursor pagination tests --------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mcp_cursor_paging_zero_pages():
    prof = make_mcp_profile(entity_overrides={"cursor_param": "cursor", "next_cursor_at": "next_cursor"})
    server = FakeMcpServer(tool_results=[{"structuredContent": {"records": [], "next_cursor": None}}])
    conn = make_connector(server, profile=prof)

    result = await conn.list_entity("REQUISITION")
    assert len(result.records) == 0
    assert server.call_count == 1


@pytest.mark.asyncio
async def test_mcp_cursor_paging_one_page():
    prof = make_mcp_profile(entity_overrides={"cursor_param": "cursor", "next_cursor_at": "next_cursor"})
    server = FakeMcpServer(
        tool_results=[{"structuredContent": {"records": [mcp_record("P1-1")], "next_cursor": None}}]
    )
    conn = make_connector(server, profile=prof)

    result = await conn.list_entity("REQUISITION")
    assert len(result.records) == 1
    assert server.call_count == 1


@pytest.mark.asyncio
async def test_mcp_cursor_paging_two_pages():
    prof = make_mcp_profile(entity_overrides={"cursor_param": "cursor", "next_cursor_at": "next_cursor"})
    server = FakeMcpServer(tool_results=[
        {"structuredContent": {"records": [mcp_record("P1-1"), mcp_record("P1-2")], "next_cursor": "cur_page2"}},
        {"structuredContent": {"records": [mcp_record("P2-1")], "next_cursor": None}},
    ])
    conn = make_connector(server, profile=prof)

    result = await conn.list_entity("REQUISITION")
    assert [r["id"] for r in result.records] == ["P1-1", "P1-2", "P2-1"]
    assert server.call_count == 2

    # Check that second tool call included the cursor param in arguments
    second_tool_req = server.requests[3]  # [0]=init, [1]=notify, [2]=call 1, [3]=call 2
    body = json.loads(second_tool_req.content.decode())
    assert body["params"]["arguments"]["cursor"] == "cur_page2"
    assert body["params"]["arguments"]["tenant"] == "primary"


@pytest.mark.asyncio
async def test_mcp_cursor_paging_too_many_pages_fails_loudly():
    prof = make_mcp_profile(entity_overrides={"cursor_param": "cursor", "next_cursor_at": "next_cursor"})
    pages = [
        {"structuredContent": {"records": [mcp_record(f"P-{i}")], "next_cursor": f"cur_{i+1}"}}
        for i in range(51)
    ]
    server = FakeMcpServer(tool_results=pages)
    conn = make_connector(server, profile=prof)

    with pytest.raises(ConnectorError) as exc:
        await conn.list_entity("REQUISITION")
    assert exc.value.code == "TOO_MANY_PAGES"
    assert server.call_count == 50


# 4. Error handling tests -----------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mcp_tool_is_error_raises_host_error_no_body_text_leaked():
    secret_leak = "DATABASE_PASSWORD_LEAKED_SECRET_HERE"
    server = FakeMcpServer(tool_is_error=True, error_message=secret_leak)
    conn = make_connector(server)

    with pytest.raises(ConnectorError) as exc:
        await conn.list_entity("REQUISITION")
    assert exc.value.code == "HOST_ERROR"
    assert secret_leak not in str(exc.value)
    assert secret_leak not in exc.value.detail


@pytest.mark.asyncio
async def test_mcp_jsonrpc_error_raises_host_error_no_body_text_leaked():
    secret_leak = "INTERNAL_RPC_PRIVATE_KEY_DATA"
    server = FakeMcpServer(jsonrpc_error={"code": -32603, "message": secret_leak})
    conn = make_connector(server)

    with pytest.raises(ConnectorError) as exc:
        await conn.list_entity("REQUISITION")
    assert exc.value.code == "HOST_ERROR"
    assert secret_leak not in str(exc.value)
    assert secret_leak not in exc.value.detail


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_val", ["not-a-list", 123, {"a": 1}, [1, 2, 3], ["not-a-dict"]])
async def test_mcp_bad_items_at_type_raises_inconsistent_payload(bad_val):
    server = FakeMcpServer(tool_results=[{"structuredContent": {"records": bad_val}}])
    conn = make_connector(server)

    with pytest.raises(ConnectorError) as exc:
        await conn.list_entity("REQUISITION")
    assert exc.value.code == "INCONSISTENT_PAYLOAD"


@pytest.mark.asyncio
async def test_mcp_non_200_http_status_raises_host_error():
    server = FakeMcpServer(tool_status=500)
    conn = make_connector(server)

    with pytest.raises(ConnectorError) as exc:
        await conn.list_entity("REQUISITION")
    assert exc.value.code == "HOST_ERROR"


@pytest.mark.asyncio
async def test_mcp_bad_json_response_raises_inconsistent_payload():
    server = FakeMcpServer(bad_json=True)
    conn = make_connector(server)

    with pytest.raises(ConnectorError) as exc:
        await conn.list_entity("REQUISITION")
    assert exc.value.code == "INCONSISTENT_PAYLOAD"


# 5. Shared conversion, rejections, and shape checks --------------------------------------------------------------

@pytest.mark.asyncio
async def test_mcp_shared_record_conversion_and_rejections():
    server = FakeMcpServer(tool_results=[{
        "structuredContent": {
            "records": [
                mcp_record("VALID-1"),
                mcp_record("REJ-1", headcount="not-an-int"),
                mcp_record("REJ-2", status="UNMAPPED_STATUS_X"),
            ]
        }
    }])
    conn = make_connector(server)
    result = await conn.list_entity("REQUISITION")

    assert len(result.records) == 1
    assert result.records[0]["id"] == "VALID-1"
    assert len(result.rejected) == 2
    rej_fields = {r["field"] for r in result.rejected}
    assert "headcount" in rej_fields
    assert "status" in rej_fields


@pytest.mark.asyncio
async def test_mcp_observed_shape():
    raw_rows = [mcp_record("R-1"), mcp_record("R-2")]
    server = FakeMcpServer(tool_results=[{"structuredContent": {"records": raw_rows}}])
    conn = make_connector(server)
    result = await conn.list_entity("REQUISITION")

    shape = observed_shape(result.rows, conn._profile.entities["REQUISITION"])
    assert "title" in shape
    assert shape["title"]["present"] is True
    assert shape["title"]["types"] == ["str"]
