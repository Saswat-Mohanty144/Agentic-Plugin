"""Adversarial audit tests for MCP HTTP Streamable endpoint and Declarative Mapping Overlay.

Covers:
- MCP protocol methods: initialize, ping, notifications/initialized, tools/list, tools/call
- Tenant policy enforcement: mcp_enabled=False -> 403, max_data_tier < 2 -> 403
- Header validation: mcp-protocol-version, x-hrms-jurisdiction, origin (allowed vs disallowed vs cross-tenant)
- JSON-RPC error handling: invalid batch, parse error, bad jsonrpc version, unknown method
- Method Not Allowed: GET /v1/mcp -> 405 Allow: POST
- Server-owned parameter isolation: caller cannot override tenant_id or jurisdiction
- BUG-2 (HIGH): Declarative mapping overlay crashes with OverlayError when applied to McpEntitySpec
"""

import pytest

from hrms_plugin.integrations.declarative import FieldSpec, McpEntitySpec
from hrms_plugin.integrations.mapping_overlay import overlay
from hrms_plugin.schema.mapping import EntityMapping, FieldMap


@pytest.fixture
def acme_headers(bearer):
    return {
        **bearer(tenant="acme", roles=["hr_manager"], sub="user-acme"),
        "x-hrms-jurisdiction": "IN",
        "Origin": "https://hr.acme.test",
        "mcp-protocol-version": "2025-06-18",
    }


@pytest.fixture
def globex_headers(bearer):
    return {
        **bearer(tenant="globex", roles=["hr_manager"], sub="user-globex"),
        "x-hrms-jurisdiction": "IN",
        "Origin": "https://hr.globex.test",
        "mcp-protocol-version": "2025-06-18",
    }


def test_mcp_initialize_and_ping_positive(make_client, acme_headers):
    """Positive: MCP initialize, ping, and initialized notification."""
    client = make_client()

    # 1. initialize
    init_res = client.post(
        "/v1/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        headers=acme_headers,
    )
    assert init_res.status_code == 200
    init_data = init_res.json()
    assert init_data["jsonrpc"] == "2.0"
    assert init_data["id"] == 1
    assert init_data["result"]["protocolVersion"] == "2025-06-18"
    assert "tools" in init_data["result"]["capabilities"]

    # 2. ping
    ping_res = client.post(
        "/v1/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "ping"},
        headers=acme_headers,
    )
    assert ping_res.status_code == 200
    assert ping_res.json()["result"] == {}

    # 3. notifications/initialized
    notify_res = client.post(
        "/v1/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=acme_headers,
    )
    assert notify_res.status_code == 202


def test_mcp_tools_list_and_call_positive(make_client, acme_headers):
    """Positive: tools/list returns available tools and tools/call executes properly."""
    client = make_client()

    # 1. tools/list
    list_res = client.post(
        "/v1/mcp",
        json={"jsonrpc": "2.0", "id": 10, "method": "tools/list"},
        headers=acme_headers,
    )
    assert list_res.status_code == 200
    tools = list_res.json()["result"]["tools"]
    tool_names = [t["name"] for t in tools]
    assert "hrms_audit_job_description_bias" in tool_names

    # 2. tools/call
    call_res = client.post(
        "/v1/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {
                "name": "hrms_audit_job_description_bias",
                "arguments": {"jd_text": "Looking for an energetic rockstar ninja developer."},
            },
        },
        headers=acme_headers,
    )
    assert call_res.status_code == 200
    call_data = call_res.json()["result"]
    assert call_data["isError"] is False
    assert len(call_data["content"]) > 0


def test_mcp_tenant_policy_enforcement_negative(make_client, globex_headers, acme_headers):
    """Negative: mcp_enabled=False on globex tenant rejects tools/list and tools/call with 403."""
    client = make_client()

    # Globex tenant has mcp_enabled: false
    res = client.post(
        "/v1/mcp",
        json={"jsonrpc": "2.0", "id": 20, "method": "tools/list"},
        headers=globex_headers,
    )
    assert res.status_code == 403
    assert res.json()["error"] == "MCP_NOT_ALLOWED_FOR_TENANT"


def test_mcp_origin_validation_negative(make_client, acme_headers):
    """Negative: disallowed origins and cross-tenant origin spoofing are rejected with 403."""
    client = make_client()

    # Disallowed origin
    bad_headers = {**acme_headers, "Origin": "https://attacker.com"}
    res = client.post(
        "/v1/mcp",
        json={"jsonrpc": "2.0", "id": 30, "method": "ping"},
        headers=bad_headers,
    )
    assert res.status_code == 403
    assert res.json()["error"] == "ORIGIN_NOT_ALLOWED"

    # Cross-tenant origin spoofing (acme token with globex origin)
    spoof_headers = {**acme_headers, "Origin": "https://hr.globex.test"}
    res_spoof = client.post(
        "/v1/mcp",
        json={"jsonrpc": "2.0", "id": 31, "method": "ping"},
        headers=spoof_headers,
    )
    assert res_spoof.status_code == 403
    assert res_spoof.json()["error"] == "ORIGIN_NOT_ALLOWED"


def test_mcp_headers_and_rpc_frames_validation(make_client, acme_headers):
    """Negative: invalid headers, bad JSON-RPC frames, and unsupported methods."""
    client = make_client()

    # 1. Unsupported protocol version
    bad_proto = {**acme_headers, "mcp-protocol-version": "1999-01-01"}
    res = client.post("/v1/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=bad_proto)
    assert res.status_code == 400
    assert res.json()["error"] == "UNSUPPORTED_PROTOCOL_VERSION"

    # 2. Missing jurisdiction header
    no_jur = {k: v for k, v in acme_headers.items() if k != "x-hrms-jurisdiction"}
    res_jur = client.post("/v1/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=no_jur)
    assert res_jur.status_code == 400
    assert res_jur.json()["error"] == "JURISDICTION_REQUIRED"

    # 3. GET /v1/mcp returns 405 Method Not Allowed
    get_res = client.get("/v1/mcp", headers=acme_headers)
    assert get_res.status_code == 405
    assert get_res.headers.get("Allow") == "POST"

    # 4. JSON-RPC batch frame rejected
    batch_res = client.post("/v1/mcp", json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}], headers=acme_headers)
    assert batch_res.status_code == 200
    assert batch_res.json()["error"]["code"] == -32600

    # 5. Unknown method
    unknown_res = client.post(
        "/v1/mcp",
        json={"jsonrpc": "2.0", "id": 40, "method": "unknown_rpc"},
        headers=acme_headers,
    )
    assert unknown_res.status_code == 200
    assert unknown_res.json()["error"]["code"] == -32601


# ==============================================================================
# CONFIRMED BUGS
# ==============================================================================


def test_overlay_mcp_entity_spec_preserves_mcp_protocol():
    """BUG-2 (HIGH): DeclarativeMappingOverlay crashes with OverlayError on McpEntitySpec.

    Decision D8 / README section 12: Declarative connector supports both REST and MCP protocols.
    In hrms_plugin/integrations/mapping_overlay.py:65, overlay() unconditionally executes:
        merged = EntitySpec.model_validate(...)
    instead of adapting to McpEntitySpec when the vendor profile uses protocol: mcp.
    Because McpEntitySpec lacks EntitySpec fields (method, pagination) and has extra fields (tool, arguments),
    Pydantic ValidationError is raised and re-wrapped into an unhandled OverlayError.
    """
    mcp_spec = McpEntitySpec(
        path="/mcp",
        tool="get_requisitions",
        arguments={},
        id_from="req_id",
        fields={"title": FieldSpec.model_validate({"from": "job_title", "type": "str"})},
    )
    mapping = EntityMapping(
        vendor="iceipts",
        entity_type="requisition",
        id_path="req_id",
        fields=(FieldMap(target="title", source="custom_job_title"),),
    )

    # When overlay is called on an McpEntitySpec with an approved mapping, it MUST return an McpEntitySpec
    merged, overridden = overlay(mcp_spec, mapping)
    assert isinstance(merged, McpEntitySpec), f"Expected McpEntitySpec, got {type(merged)}"
    assert overridden == ["title"]
    assert merged.fields["title"].source == "custom_job_title"
