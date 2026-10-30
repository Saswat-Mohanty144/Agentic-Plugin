"""FastAPI routes for MCP Streamable HTTP transport (decision D8)."""

from __future__ import annotations

import json
import math
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from hrms_plugin.security.http_auth import ApiError, require
from hrms_plugin.security.rbac import AuthContext, Permission
from hrms_plugin.server.mcp_server import JURISDICTIONS, HrmsMcpServer

router = APIRouter()


def _rate_limit(request: Request, auth: AuthContext) -> None:
    retry = request.app.state.rate_limiter.check(auth.tenant_id)
    if retry is not None:
        raise ApiError(429, {"error": "RATE_LIMITED"}, headers={"Retry-After": str(max(1, math.ceil(retry)))})


def _rpc_error(req_id: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=200,
        content={"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}},
    )


def _rpc_ok(req_id: Any, result: Any) -> JSONResponse:
    return JSONResponse(
        status_code=200,
        content={"jsonrpc": "2.0", "id": req_id, "result": result},
    )


@router.get("/v1/mcp")
def mcp_get() -> Response:
    return Response(status_code=405, headers={"Allow": "POST"})


@router.post("/v1/mcp")
async def mcp_post(
    request: Request,
    auth: AuthContext = Depends(require(Permission.USE_COPILOT)),
) -> Response:
    _rate_limit(request, auth)

    origin = request.headers.get("origin")
    if origin is not None and origin not in request.app.state.registry.all_origins():
        raise ApiError(403, {"error": "ORIGIN_NOT_ALLOWED"})

    jurisdiction = request.headers.get("x-hrms-jurisdiction")
    if not jurisdiction or jurisdiction not in JURISDICTIONS:
        raise ApiError(400, {"error": "JURISDICTION_REQUIRED"})

    raw = await request.body()
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _rpc_error(None, -32700, "Parse error")

    if isinstance(payload, list):
        return _rpc_error(None, -32600, "Invalid Request: batches not supported")
    if not isinstance(payload, dict):
        return _rpc_error(None, -32600, "Invalid Request: expected object")

    has_id = "id" in payload
    raw_id = payload.get("id")
    if isinstance(raw_id, bool) or (has_id and not isinstance(raw_id, (str, int, float, type(None)))):
        return _rpc_error(None, -32600, "Invalid Request: id must be a string, number, or null")
    req_id = raw_id if has_id else None

    if payload.get("jsonrpc") != "2.0":
        return _rpc_error(req_id, -32600, "Invalid Request: jsonrpc must be '2.0'")

    method = payload.get("method")
    if not isinstance(method, str):
        return _rpc_error(req_id, -32600, "Invalid Request: method must be a string")

    if method == "notifications/initialized" or not has_id:
        return Response(status_code=202)

    if method == "initialize":
        params = payload.get("params") or {}
        client_ver = params.get("protocolVersion") if isinstance(params, dict) else None
        version = client_ver if client_ver in ("2025-06-18", "2025-03-26") else "2025-06-18"
        return _rpc_ok(req_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "hrms-agentic-plugin", "version": "2.0.0"},
        })

    if method == "ping":
        return _rpc_ok(req_id, {})

    if method == "tools/list":
        server = HrmsMcpServer(auth=auth, jurisdiction=jurisdiction)
        tools = [
            {"name": t.name, "description": t.description, "inputSchema": t.input_schema}
            for t in server.list_tools()
        ]
        return _rpc_ok(req_id, {"tools": tools})

    if method == "tools/call":
        params = payload.get("params")
        if not isinstance(params, dict):
            return _rpc_error(req_id, -32602, "Invalid params: expected object")
        name = params.get("name")
        if not isinstance(name, str):
            return _rpc_error(req_id, -32602, "Invalid params: name must be a string")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        elif not isinstance(arguments, dict):
            return _rpc_error(req_id, -32602, "Invalid params: arguments must be an object")

        server = HrmsMcpServer(auth=auth, jurisdiction=jurisdiction)
        output = await server.call_tool(name, arguments)
        is_error = bool(output.get("success") is False or "error" in output)
        return _rpc_ok(req_id, {
            "content": [{"type": "text", "text": json.dumps(output, default=str)}],
            "structuredContent": output,
            "isError": is_error,
        })

    return _rpc_error(req_id, -32601, f"Method not found: {method}")
