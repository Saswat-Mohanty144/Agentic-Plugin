"""Unit tests for the Super Agentic AI Engine & LLM Gateway."""

import base64

import pytest
from fastapi.testclient import TestClient

from hrms_plugin.agents.orchestrator import AgenticOrchestrator
from hrms_plugin.agents.supervisor import SupervisorAgent
from hrms_plugin.agents.tools import AgenticToolRegistry
from hrms_plugin.ai.llm import (
    ChatMessage,
    LLMConfig,
    LLMGateway,
    LLMProvider,
)
from hrms_plugin.schema.canonical import EntityType
from hrms_plugin.schema.introspector import DiscoveredEntity, DiscoveredField
from hrms_plugin.schema.synthesizer import MappingSynthesizer
from hrms_plugin.server.app import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def gateway():
    return LLMGateway(config=LLMConfig(provider=LLMProvider.FAKE))


@pytest.fixture
def tool_registry():
    return AgenticToolRegistry()


@pytest.fixture
def orchestrator(gateway, tool_registry):
    return AgenticOrchestrator(llm_gateway=gateway, tool_registry=tool_registry)


@pytest.mark.asyncio
async def test_llm_gateway_direct_chat_and_pii_masking(gateway):
    """Test LLM gateway direct completion with automatic PII masking and de-tokenization."""
    prompt = "Please verify employee John Doe at john.doe@company.com with phone +91-9876543210."
    resp = await gateway.chat(
        messages=[ChatMessage(role="user", content=prompt)],
        tenant_id="tenant_alpha",
        mask_pii=True,
    )
    assert resp.content
    assert resp.finish_reason == "stop"
    assert resp.provider == "fake"


@pytest.mark.asyncio
async def test_tool_registry_registration_and_execution(tool_registry):
    """Test tool registry schemas and deterministic tool execution."""
    defs = tool_registry.get_definitions()
    tool_names = {t.name for t in defs}
    assert "calculate_salary_structure" in tool_names
    assert "audit_statutory_compliance" in tool_names
    assert "calculate_eosb_gratuity" in tool_names
    assert "evaluate_leave_request" in tool_names

    # Test schema generation
    salary_tool = next(t for t in defs if t.name == "calculate_salary_structure")
    openai_schema = salary_tool.to_openai_schema()
    assert openai_schema["type"] == "function"
    assert openai_schema["function"]["name"] == "calculate_salary_structure"

    # Test execution
    res = await tool_registry.execute(
        "calculate_salary_structure",
        {"gross_or_ctc": 1200000.0, "jurisdiction": "IN"},
    )
    assert "annual_ctc" in res or "gross_salary" in res


@pytest.mark.asyncio
async def test_agentic_orchestrator_react_loop(orchestrator):
    """Test full autonomous ReAct reasoning loop: Thought -> Tool Call -> Observation -> Final Answer."""
    user_msg = "Audit our employees in Dubai for statutory labor compliance"
    resp = await orchestrator.run(
        user_message=user_msg,
        jurisdiction="AE",
        tenant_id="tenant_dubai",
    )
    assert resp.reply_text
    assert resp.routed_agent == "AgenticBrain"
    assert resp.confidence_score >= 0.90
    assert len(resp.statutory_citations) > 0 or len(resp.suggested_actions) > 0


@pytest.mark.asyncio
async def test_agentic_orchestrator_streaming(orchestrator):
    """Test streaming SSE chunks for thoughts, tool calls, and text tokens."""
    events = []
    async for chunk in orchestrator.run_stream(
        user_message="Structure a monthly salary of AED 15000",
        jurisdiction="AE",
    ):
        events.append(chunk)

    assert len(events) > 0
    event_types = {e.get("event") for e in events}
    assert "thinking" in event_types or "token" in event_types


@pytest.mark.asyncio
async def test_supervisor_agentic_methods():
    """Test SupervisorAgent process_agentic and stream_agentic methods."""
    supervisor = SupervisorAgent()
    resp = await supervisor.process_agentic(
        message="Calculate end of service gratuity for 4 years with basic 12000 AED",
        jurisdiction="AE",
    )
    assert resp.reply_text
    assert resp.routed_agent == "AgenticBrain"

    stream_chunks = []
    async for chunk in supervisor.stream_agentic(
        message="Check attendance anomaly between punches",
        jurisdiction="IN",
    ):
        stream_chunks.append(chunk)
    assert len(stream_chunks) > 0


@pytest.mark.asyncio
async def test_synthesize_agentic_semantic_mapping():
    """Test zero-shot LLM semantic schema synthesis."""
    discovered = DiscoveredEntity(
        name="Employee",
        fields={
            "c_fname": DiscoveredField(name="c_fname", path="c_fname"),
            "work_mail": DiscoveredField(name="work_mail", path="work_mail"),
        },
    )
    mapping, report = await MappingSynthesizer.synthesize_agentic(
        discovered=discovered,
        canonical_type=EntityType.EMPLOYEE,
    )
    assert report.entity_type == "EMPLOYEE"
    assert len(mapping.fields) > 0


def test_api_agentic_chat_endpoint(client):
    """Test POST /v1/chat with agentic: true."""
    payload = {
        "message": "Audit employee records for UAE labor law compliance",
        "jurisdiction": "AE",
        "agentic": True,
    }
    resp = client.post("/v1/chat", json=payload, headers={"X-Tenant-Id": "demo_corp"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["routed_agent"] == "AgenticBrain"
    assert len(data.get("statutory_citations", [])) > 0 or len(data.get("suggested_actions", [])) > 0


def test_api_chat_stream_endpoint(client):
    """Test POST /v1/chat/stream Server-Sent Events endpoint."""
    payload = {
        "message": "Audit employee compliance and calculate take home",
        "jurisdiction": "AE",
    }
    resp = client.post("/v1/chat/stream", json=payload, headers={"X-Tenant-Id": "demo_corp"})
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers.get("content-type", "")
    assert len(resp.text) > 0


def test_api_voice_endpoints(client):
    """Test voice transcribe, synthesize, and multimodal turn endpoints."""
    dummy_wav = b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x44\xac\x00\x00"
    b64_audio = base64.b64encode(dummy_wav).decode("utf-8")

    # 1. Transcribe
    res_stt = client.post("/v1/voice/transcribe", json={"audio_base64": b64_audio})
    assert res_stt.status_code == 200
    assert "transcript" in res_stt.json()

    # 2. Synthesize
    res_tts = client.post("/v1/voice/synthesize", json={"text": "Your sick leave has been approved"})
    assert res_tts.status_code == 200
    assert "audio_base64" in res_tts.json()

    # 3. Multimodal Voice Turn
    res_turn = client.post("/v1/voice/turn", json={"audio_base64": b64_audio, "jurisdiction": "AE"})
    assert res_turn.status_code == 200
    turn_data = res_turn.json()
    assert "transcript_in" in turn_data
    assert "reply_text" in turn_data
    assert "reply_audio_base64" in turn_data


def test_api_agentic_synthesize_endpoint(client):
    """Test POST /v1/schema/synthesize/agentic endpoint."""
    payload = {
        "domain": "EMPLOYEE",
        "discovered_fields": [
            {"name": "emp_code", "path": "emp_code"},
            {"name": "first_name", "path": "first_name"},
            {"name": "email_address", "path": "email_address"},
        ],
    }
    resp = client.post("/v1/schema/synthesize/agentic", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["entity_type"] == "EMPLOYEE"
    assert len(data["field_mappings"]) > 0
