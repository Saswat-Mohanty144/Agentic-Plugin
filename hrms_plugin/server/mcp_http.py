"""FastAPI routes for MCP Streamable HTTP transport (decision D8)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from hrms_plugin.governance import disclosure
from hrms_plugin.governance.rate_limit import enforce_rate_limit
from hrms_plugin.security.http_auth import ApiError, require
from hrms_plugin.security.rbac import AuthContext, Permission
from hrms_plugin.server.mcp_server import JURISDICTIONS, HrmsMcpServer

router = APIRouter()


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
    enforce_rate_limit(request, auth)

    proto_version = request.headers.get("mcp-protocol-version")
    if proto_version is not None and proto_version not in ("2025-06-18", "2025-03-26"):
        raise ApiError(400, {"error": "UNSUPPORTED_PROTOCOL_VERSION"})

    tenant = next((t for t in request.app.state.registry.tenants if t.tenant_id == auth.tenant_id), None)
    origin = request.headers.get("origin")
    if origin is not None:
        allowed = tenant.allowed_origins if tenant else ()
        if origin not in allowed:
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

    if method in ("tools/list", "tools/call"):
        policy = tenant.llm_policy if tenant else None
        if not policy or not policy.mcp_enabled or policy.max_data_tier < 2:
            raise ApiError(403, {"error": "MCP_NOT_ALLOWED_FOR_TENANT"})

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

        egress_log = getattr(request.app.state, "egress_log", None)
        if egress_log is None:
            try:
                from hrms_plugin.server.app import egress_log as default_egress_log
                egress_log = default_egress_log
            except Exception:
                egress_log = None

        key = None
        if egress_log is not None:
            payload_sha256 = "sha256:" + hashlib.sha256(
                json.dumps(arguments, sort_keys=True, default=str).encode()
            ).hexdigest()
            key = egress_log.record(
                tenant_id=auth.tenant_id,
                provider="mcp-client",
                model=name,
                payload_sha256=payload_sha256,
                redacted_kinds={},
                status="SENT",
                channel="mcp",
                subject_refs=[],
            )

        server = HrmsMcpServer(auth=auth, jurisdiction=jurisdiction)
        try:
            output = await server.call_tool(name, arguments)
            safe_output = json.loads(json.dumps(output, default=str))
            is_error = bool(output.get("success") is False or "error" in output)
            if egress_log is not None and key is not None:
                if is_error:
                    egress_log.update(key, status="FAILED")
                else:
                    subject_refs = disclosure.extract_disclosed_refs(auth.tenant_id, safe_output)
                    egress_log.update(key, status="COMPLETED", subject_refs=subject_refs)
            return _rpc_ok(req_id, {
                "content": [{"type": "text", "text": json.dumps(safe_output)}],
                "structuredContent": safe_output,
                "isError": is_error,
            })
        except Exception:
            if egress_log is not None and key is not None:
                egress_log.update(key, status="FAILED")
            raise

    return _rpc_error(req_id, -32601, f"Method not found: {method}")
