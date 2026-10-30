"""FastAPI REST Service Surface for the Standalone Agentic HRMS AI Plugin."""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Mapping, Optional

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

from hrms_plugin.agents.compliance import ComplianceAgent
from hrms_plugin.agents.leave_attendance import LeaveAttendanceAgent
from hrms_plugin.agents.recruitment import RecruitmentAgent
from hrms_plugin.agents.statutory_payroll import StatutoryPayrollAgent
from hrms_plugin.agents.supervisor import AgentResponse, SupervisorAgent
from hrms_plugin.agents.tools import STATUTORY_JURISDICTIONS
from hrms_plugin.agents.voice import STT_MODEL, TTS_MODEL, VoiceAgent, VoiceUpstreamError
from hrms_plugin.ai.llm import LLMNotConfigured
from hrms_plugin.governance import disclosure
from hrms_plugin.governance.egress_log import EgressLog
from hrms_plugin.governance.llm_policy import LlmBudgetExceeded, LlmNotAllowed
from hrms_plugin.governance.rate_limit import TenantRateLimiter, enforce_rate_limit
from hrms_plugin.payroll.service import PayrollService
from hrms_plugin.rag.store import StatutoryKnowledgeBase
from hrms_plugin.schema.canonical import EntityType
from hrms_plugin.schema.introspector import SchemaIntrospector
from hrms_plugin.schema.synthesizer import MappingSynthesizer
from hrms_plugin.security.audit import AuditSigningError, HostAuditGateway
from hrms_plugin.security.http_auth import ApiError, require
from hrms_plugin.security.jwks_cache import JwksCache, make_httpx_fetcher
from hrms_plugin.security.pii import PiiEgressBlocked, PiiSession
from hrms_plugin.security.rbac import AuthContext, Permission, PermissionDenied
from hrms_plugin.security.tenant_registry import TenantRegistry, load_registry_from_env
from hrms_plugin.security.token_verifier import TokenVerifier
from hrms_plugin.server.mcp_http import router as mcp_router
from hrms_plugin.storage.base import BaseKeyValueStore
from hrms_plugin.storage.mapping_store import MappingGovernanceError, MappingStore
from hrms_plugin.storage.sqlite_store import SqliteKeyValueStore
from hrms_plugin.writeback.service import WritebackError, WritebackService

API_DOCS_ENV_VAR = "HRMS_ENABLE_API_DOCS"
REMEDIATION_MAX_TOKEN_AGE_SECONDS = 300
JurisdictionCode = Literal["IN", "AE", "SA", "US"]



# Core singletons
kb = StatutoryKnowledgeBase()
payroll_agent = StatutoryPayrollAgent(kb=kb)
compliance_agent = ComplianceAgent(kb=kb)
leave_agent = LeaveAttendanceAgent()
recruitment_agent = RecruitmentAgent()
supervisor = SupervisorAgent(
    kb=kb,
    payroll_agent=payroll_agent,
    compliance_agent=compliance_agent,
    leave_agent=leave_agent,
    recruitment_agent=recruitment_agent,
)
VOICE_PROVIDER = "sarvam"


def voice_agent_from_env(env: Mapping[str, str]) -> VoiceAgent:
    """Real Sarvam only with SARVAM_API_KEY; without it the voice routes are 503 (VOICE-1, V5)."""
    return VoiceAgent(api_key=(env.get("SARVAM_API_KEY") or "").strip() or None)


voice_agent = voice_agent_from_env(os.environ)
logger = logging.getLogger(__name__)

# Persistent storage singletons
_storage = SqliteKeyValueStore(
    str(Path((os.environ.get("HRMS_DATA_DIR") or "").strip() or ".hrms_data") / "plugin_storage.db")
)


LEGACY_PII_NAMESPACES = ("pii_vault", "pii_vault_lookup")


def purge_legacy_pii_vault(store: BaseKeyValueStore) -> None:
    """Delete plaintext PII written by the removed persistent vault (piece C, decision C5)."""
    for namespace in LEGACY_PII_NAMESPACES:
        store.clear(namespace)


purge_legacy_pii_vault(_storage)
payroll_service = PayrollService(store=_storage)
writeback_service = WritebackService(store=_storage)
egress_log = EgressLog(_storage)
mapping_store = MappingStore(backend=_storage)


def make_classification_resolver(
    store: MappingStore,
) -> Callable[[str, str], Optional[Dict[str, Any]]]:
    """Resolve active field classification for a tenant and entity type using the tenant's host vendor."""

    def classification_for(tenant_id: str, entity: str) -> Optional[Dict[str, Any]]:
        if not os.environ.get("HRMS_HOSTS"):
            return None
        try:
            from hrms_plugin.integrations.host_registry import load_host_registry_from_env

            host = load_host_registry_from_env().for_tenant(tenant_id)
            if not host:
                return None
            return store.active_classification(tenant_id, host.vendor, entity)
        except Exception:
            logger.warning("Failed to resolve field classification")
            return None

    return classification_for


def _resolve_vendor(tenant_id: str, vendor: Optional[str]) -> str:
    """Use explicit vendor if provided, else tenant's host vendor from HRMS_HOSTS, else 'host'."""
    if vendor:
        return vendor
    if os.environ.get("HRMS_HOSTS"):
        try:
            from hrms_plugin.integrations.host_registry import load_host_registry_from_env

            host = load_host_registry_from_env().for_tenant(tenant_id)
            if host and host.vendor:
                return host.vendor
        except Exception:
            pass
    return "host"


classification_for = make_classification_resolver(mapping_store)
supervisor.classification_for = classification_for

audit_gateway = HostAuditGateway()
introspector = SchemaIntrospector()
synthesizer = MappingSynthesizer()


# Request size limits (token waste #7): one request must never fill the context window.
MESSAGE_MAX_CHARS = 8000
HISTORY_BUDGET_CHARS = 24000


def check_message(text: str) -> None:
    if len(text) > MESSAGE_MAX_CHARS:
        raise ApiError(422, {"error": "MESSAGE_TOO_LONG", "max_chars": MESSAGE_MAX_CHARS})


def trim_history(history: List[Dict[str, str]]) -> "tuple[List[Dict[str, str]], int]":
    """Drop the oldest turns until the history fits the budget; what remains starts with a user turn."""
    start = 0
    total = sum(len(h["content"]) for h in history)
    while start < len(history) and (total > HISTORY_BUDGET_CHARS or history[start]["role"] != "user"):
        total -= len(history[start]["content"])
        start += 1
    return history[start:], start


# Request / Response Schemas
class HistoryItem(BaseModel):
    """Prior conversation turn supplied by the client. Only user/assistant roles: a client must never be able to
    inject operator (system) or tool turns (piece E8)."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=32000)


class ChatRequest(BaseModel):
    message: str
    jurisdiction: JurisdictionCode = "IN"
    context: Optional[Dict[str, Any]] = None
    mask_pii: bool = True  # deprecated: protection is always on (piece C, decision C4)
    agentic: bool = True
    history: Optional[List[HistoryItem]] = Field(default=None, max_length=50)

    @field_validator("history")
    @classmethod
    def _history_starts_with_user(cls, value: Optional[List[HistoryItem]]) -> Optional[List[HistoryItem]]:
        if value and value[0].role != "user":
            raise ValueError("history must start with a user turn")
        return value


class VoiceTranscribeRequest(BaseModel):
    audio_base64: str
    language_code: Optional[str] = "en-IN"


class VoiceSynthesizeRequest(BaseModel):
    text: str
    target_language: Optional[str] = "en-IN"
    speaker: Optional[str] = "meera"


class VoiceTurnRequest(BaseModel):
    audio_base64: str
    user_id: str = "USR-001"
    jurisdiction: JurisdictionCode = "IN"
    language_code: Optional[str] = "en-IN"


class IntrospectRequest(BaseModel):
    swagger_dict: Optional[Dict[str, Any]] = None
    sample_records: Optional[Dict[str, List[Dict[str, Any]]]] = None
    vendor: Optional[str] = Field(default=None, pattern=r"^[a-z0-9_-]{1,32}$")


class SynthesizeRequest(BaseModel):
    domain: str = "EMPLOYEE"
    discovered_fields: List[Dict[str, Any]]
    vendor: Optional[str] = Field(default=None, pattern=r"^[a-z0-9_-]{1,32}$")
    sample_records: Optional[List[Dict[str, Any]]] = Field(default=None, max_length=50)


class FingerprintApproveRequest(BaseModel):
    """Approve the observed host schema fingerprint so payroll calculations may run (piece F1-7)."""

    model_config = ConfigDict(extra="forbid")

    vendor: Literal["frappe"]
    doctype: Literal["Salary Slip"]
    fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class WritebackProposalRequest(BaseModel):
    """Propose a field-level change to a host record; a different person must approve it (piece F2)."""

    model_config = ConfigDict(extra="forbid")

    doctype: str = Field(min_length=1, max_length=140)
    name: str = Field(min_length=1, max_length=140)
    patch: Dict[str, Any]


WRITEBACK_STATUS = {
    "NOT_FOUND": 404, "FIELD_NOT_WRITABLE": 422, "INVALID_VALUE": 422, "NO_CHANGE": 422, "CONFLICT": 409,
    "ROLLBACK_CONFLICT": 409, "ROLLBACK_UNAVAILABLE": 409, "EXPIRED": 409, "INVALID_STATE": 409, "SAME_PERSON": 403,
    "HOST_NOT_CONFIGURED": 503, "HOST_ERROR": 502, "VENDOR_NOT_SUPPORTED": 409, "AUDIT_SIGNING_NOT_CONFIGURED": 503,
}


class RemediationExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    violation_id: str
    target_entity: str
    entity_id: str
    patch_payload: Dict[str, Any]


class DisclosureLookupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1)
    value: str = Field(min_length=1, max_length=256)

    @field_validator("kind")
    @classmethod
    def validate_kind(cls, v: str) -> str:
        kind_upper = v.upper()
        if kind_upper not in disclosure.VALID_KINDS:
            raise ValueError(f"Invalid kind: {v}")
        return kind_upper


router = APIRouter()


@router.get("/healthz")
def healthz():
    return {
        "status": "healthy",
        "version": "2.0.0",
        "jurisdictions_supported": ["IN", "AE", "SA", "US"],
    }


@router.post("/v1/chat", response_model=AgentResponse)
async def chat_endpoint(
    req: ChatRequest,
    request: Request,
    auth: AuthContext = Depends(require(Permission.USE_COPILOT)),
):
    """Unified conversational AI copilot endpoint with automatic PII masking and agentic tool reasoning."""
    enforce_rate_limit(request, auth)
    check_message(req.message)
    history, trimmed = trim_history([h.model_dump() for h in req.history or []])
    agent: SupervisorAgent = request.app.state.supervisor
    text_to_process = req.message  # PII is redacted at the LLM gateway, not here (piece C)

    if req.agentic:
        response = await agent.process_agentic(
            message=text_to_process,
            jurisdiction=req.jurisdiction,
            context=req.context,
            history=history or None,
            auth=auth,
        )
    else:
        response = agent.process_message(
            message=text_to_process,
            jurisdiction=req.jurisdiction,
            context=req.context,
            auth=auth,
        )
    response.history_trimmed = trimmed
    return response


@router.post("/v1/chat/stream")
async def chat_stream_endpoint(
    req: ChatRequest,
    request: Request,
    auth: AuthContext = Depends(require(Permission.USE_COPILOT)),
):
    """Real-time SSE event stream for autonomous agent thoughts, tool dispatches, and tokens."""
    enforce_rate_limit(request, auth)
    check_message(req.message)
    history, trimmed = trim_history([h.model_dump() for h in req.history or []])
    agent: SupervisorAgent = request.app.state.supervisor
    # Resolve LLM configuration, tenant policy and budget before streaming starts → plain 503/403/429
    # (agy E-F7, pieces G2 and H4)
    agent.get_orchestrator().llm_gateway.preflight(auth.tenant_id)

    async def event_generator():
        text_to_process = req.message  # PII is redacted at the LLM gateway, not here (piece C)
        if trimmed:
            yield f"data: {json.dumps({'event': 'history_trimmed', 'data': trimmed})}\n\n"

        try:
            async for chunk in agent.stream_agentic(
                message=text_to_process,
                jurisdiction=req.jurisdiction,
                context=req.context,
                history=history or None,
                auth=auth,
            ):
                yield f"data: {json.dumps(chunk, default=str)}\n\n"
        except LLMNotConfigured:
            yield f"data: {json.dumps({'event': 'error', 'data': {'error': 'LLM_NOT_CONFIGURED'}})}\n\n"
        except LlmBudgetExceeded:  # budget ran out between ReAct turns
            yield f"data: {json.dumps({'event': 'error', 'data': {'error': 'LLM_BUDGET_EXHAUSTED'}})}\n\n"
        except PiiEgressBlocked as exc:
            # HTTP 200 is already committed once streaming starts, so report the block in-band (agy F2).
            blocked = {"event": "error", "data": {"error": "PII_EGRESS_BLOCKED", "kinds": exc.kinds}}
            yield f"data: {json.dumps(blocked)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


def _decode_audio(audio_base64: str) -> bytes:
    """Strict base64: malformed client audio is a 422, never a 500 (E2E-GOV-1)."""
    try:
        return base64.b64decode(audio_base64, validate=True)
    except (binascii.Error, ValueError):
        raise ApiError(422, {"error": "INVALID_AUDIO_BASE64"}) from None


def _entity_type(domain: str) -> EntityType:
    """An unknown synthesis domain is a 422, never a 500 (E2E-GOV-2)."""
    try:
        return EntityType(domain.upper())
    except ValueError:
        raise ApiError(422, {"error": "INVALID_DOMAIN"}) from None


def _policy_limit(registry: TenantRegistry) -> Callable[[str], Optional[int]]:
    def resolve(tenant_id: str) -> Optional[int]:
        tenant = next((t for t in registry.tenants if t.tenant_id == tenant_id), None)
        return tenant.llm_policy.requests_per_minute if tenant and tenant.llm_policy else None

    return resolve


_rate_limit = enforce_rate_limit


def _require_demo(request: Request) -> None:
    """Compliance views without a host data source never present sample data as the tenant's own (P1/P2)."""
    if not request.app.state.demo_endpoints:
        raise ApiError(503, {"error": "COMPLIANCE_SOURCE_NOT_CONNECTED"})


def _require_voice_opt_in(request: Request, tenant_id: str) -> None:
    """V1: a tenant's audio and text reach the speech provider only when its policy lists it (fail closed)."""
    tenant = next((t for t in request.app.state.registry.tenants if t.tenant_id == tenant_id), None)
    policy = tenant.llm_policy if tenant else None
    if policy is None or VOICE_PROVIDER not in policy.voice_providers:
        raise ApiError(403, {"error": "VOICE_NOT_ALLOWED_FOR_TENANT"})


@contextlib.asynccontextmanager
async def _voice_egress(tenant_id: str, model: str, payload: bytes,
                        subject_refs: Optional[List[Dict[str, Any]]] = None):
    """V2: every real Sarvam call is logged before it leaves; nothing is logged when nothing is sent."""
    if not voice_agent.api_key:
        yield
        return
    key = egress_log.record(
        tenant_id=tenant_id,
        provider=VOICE_PROVIDER,
        model=model,
        payload_sha256="sha256:" + hashlib.sha256(payload).hexdigest(),
        redacted_kinds={},
        channel="voice",
        subject_refs=list(subject_refs or []),
    )
    try:
        yield
    except BaseException:
        egress_log.update(key, status="FAILED")
        raise
    egress_log.update(key, status="COMPLETED")


async def _transcribe(request: Request, auth: AuthContext, audio: bytes, language_code: Optional[str]) -> Dict:
    """V4: audio cannot be redacted, so the tenant's opt-in is the gate; the transcript is redacted by the LLM layer."""
    async with _voice_egress(auth.tenant_id, STT_MODEL, audio, subject_refs=[]):
        return await voice_agent.transcribe_audio(audio, language_code=language_code, model=STT_MODEL)


async def _synthesize(auth: AuthContext, text: str, **kw: Any) -> Dict:
    """V3: spoken text cannot carry tokens, so detectable PII is never sent (PiiEgressBlocked)."""
    kinds = PiiSession().find_pii(text)
    if kinds:
        raise PiiEgressBlocked(kinds)
    tts_refs = disclosure.extract_disclosed_refs(auth.tenant_id, text) if disclosure.is_configured() else []
    async with _voice_egress(auth.tenant_id, TTS_MODEL, text.encode(), subject_refs=tts_refs):
        return await voice_agent.synthesize_speech(text=text, model=TTS_MODEL, **kw)


@router.post("/v1/voice/transcribe")
async def voice_transcribe(
    req: VoiceTranscribeRequest, request: Request, auth: AuthContext = Depends(require(Permission.USE_COPILOT))
):
    """Transcribe base64 audio bytes via Sarvam AI Saaras model."""
    enforce_rate_limit(request, auth)
    audio_bytes = _decode_audio(req.audio_base64)
    _require_voice_opt_in(request, auth.tenant_id)
    return await _transcribe(request, auth, audio_bytes, req.language_code)


@router.post("/v1/voice/synthesize")
async def voice_synthesize(
    req: VoiceSynthesizeRequest, request: Request, auth: AuthContext = Depends(require(Permission.USE_COPILOT))
):
    """Synthesize text into natural voice audio via Sarvam AI Bulbul model."""
    enforce_rate_limit(request, auth)
    check_message(req.text)
    _require_voice_opt_in(request, auth.tenant_id)
    return await _synthesize(auth, req.text, target_language=req.target_language, speaker=req.speaker)


@router.post("/v1/voice/turn")
async def voice_turn(
    req: VoiceTurnRequest,
    request: Request,
    auth: AuthContext = Depends(require(Permission.USE_COPILOT)),
):
    """Full-duplex multimodal turn: Audio In -> Transcribe -> ReAct Agent Brain -> Spoken Audio Out."""
    enforce_rate_limit(request, auth)
    audio_bytes = _decode_audio(req.audio_base64)
    _require_voice_opt_in(request, auth.tenant_id)
    stt = await _transcribe(request, auth, audio_bytes, req.language_code)
    transcript = stt.get("transcript", "")

    # Run agentic turn
    agent_res = await request.app.state.supervisor.process_agentic(
        message=transcript,
        jurisdiction=req.jurisdiction,
        auth=auth,
    )

    # Synthesize spoken voice reply; a reply carrying PII is shown but not spoken (V3)
    audio_withheld = None
    try:
        tts = await _synthesize(auth, agent_res.reply_text, target_language=stt.get("language_code", "en-IN"))
    except PiiEgressBlocked:
        tts, audio_withheld = {}, "PII_EGRESS_BLOCKED"

    return {
        "transcript_in": transcript,
        "reply_text": agent_res.reply_text,
        "reply_audio_base64": tts.get("audio_base64"),
        "audio_withheld": audio_withheld,
        "routed_agent": agent_res.routed_agent,
        "statutory_citations": agent_res.statutory_citations,
        "suggested_actions": agent_res.suggested_actions,
    }


@router.post("/v1/schema/introspect")
def introspect_schema(req: IntrospectRequest, auth: AuthContext = Depends(require(Permission.MANAGE_SCHEMA))):
    """Introspect Swagger 2.0 / OpenAPI spec or sample JSON payloads."""
    if req.swagger_dict:
        discovered = introspector.introspect_openapi(req.swagger_dict)
        return {
            "title": discovered.title,
            "version": discovered.version,
            "entities_discovered": list(discovered.entities.keys()),
            "endpoints_count": discovered.endpoints_count,
        }
    elif req.sample_records:
        from hrms_plugin.schema.drift import SchemaDriftInterceptor

        vendor = _resolve_vendor(auth.tenant_id, req.vendor)
        entities = {}
        drift_events = []
        interceptor = SchemaDriftInterceptor()
        for entity_name, samples in req.sample_records.items():
            discovered_ent = introspector.introspect_json_samples(entity_name, samples)
            # Structure only (Q3): sample values, and "enums" inferred from them, are data and never leave.
            entities[entity_name] = discovered_ent.model_dump(
                exclude={"raw_sample": True, "fields": {"__all__": {"sample_values", "enum_values"}}})
            try:
                active_res = mapping_store.active_version(auth.tenant_id, vendor, entity_name)
            except MappingGovernanceError as exc:
                raise ApiError(400, {"error": exc.code}) from None
            if active_res is not None:
                _, active_mapping = active_res
                try:
                    domain_type = _entity_type(entity_name)
                except ApiError:
                    domain_type = None
                if domain_type is not None:
                    combined_resp: Dict[str, Any] = {}
                    for s in samples:
                        if isinstance(s, dict):
                            combined_resp.update(s)
                    events = interceptor.inspect_response(
                        entity_type=domain_type,
                        mapping=active_mapping,
                        raw_response=combined_resp,
                    )
                    for ev in events:
                        patch = dict(ev.suggested_patch) if ev.suggested_patch else None
                        if patch:
                            patch.pop("sample_value", None)
                        drift_events.append({
                            "severity": ev.severity.value,
                            "entity_type": ev.entity_type.value,
                            "detected_at": ev.detected_at,
                            "field_name": ev.field_name,
                            "description": ev.description,
                            "suggested_patch": patch,
                            "resolved": ev.resolved,
                        })
        return {
            "entities_discovered": list(entities.keys()),
            "entities": entities,
            "drift": drift_events,
        }
    else:
        raise HTTPException(status_code=400, detail="Must provide swagger_dict or sample_records")


def _prepare_synthesis_discovered(domain_value: str, req: SynthesizeRequest):
    from hrms_plugin.schema.introspector import DiscoveredEntity, DiscoveredField

    sample_ent = (
        introspector.introspect_json_samples(domain_value, req.sample_records)
        if req.sample_records
        else None
    )
    discovered_fields_dict = {}
    for f in req.discovered_fields:
        name = f["name"]
        enums = f.get("enum_values")
        if enums is None and sample_ent and name in sample_ent.fields:
            enums = sample_ent.fields[name].enum_values
        discovered_fields_dict[name] = DiscoveredField(
            name=name,
            path=f.get("path", name),
            enum_values=enums or [],
        )
    return DiscoveredEntity(name=domain_value, fields=discovered_fields_dict)


def _finalize_synthesis(
    auth: AuthContext,
    mapping: Any,
    report: Any,
    sample_records: Optional[List[Dict[str, Any]]],
) -> Dict[str, Any]:
    from hrms_plugin.schema.prober import ShadowProber

    probe_data = None
    if sample_records is not None:
        report_probe = ShadowProber.probe(mapping, sample_records)
        issues = []
        for i in report_probe.issues:
            msg = i.message
            if msg.startswith("Unexpected exception during projection:"):
                msg = "Exception"
            issues.append({
                "severity": i.severity.value if hasattr(i.severity, "value") else str(i.severity),
                "field": i.field,
                "sample_index": i.sample_index,
                "message": msg,
            })
        probe_data = {
            "is_valid": report_probe.is_valid,
            "quality_score": report_probe.quality_score,
            "records_tested": report_probe.records_tested,
            "issues": issues,
        }
    coverage_data = getattr(report, "coverage", None)
    version = mapping_store.propose_mapping(
        auth.tenant_id, mapping, proposed_by=auth.user_id, probe=probe_data, coverage=coverage_data
    )
    res = {**report.model_dump(), "mapping_version": version, "mapping_status": "PROPOSED"}
    if probe_data is not None:
        res["probe"] = probe_data
    return res


@router.post("/v1/schema/synthesize")
def synthesize_mapping(
    req: SynthesizeRequest,
    auth: AuthContext = Depends(require(Permission.MANAGE_SCHEMA)),
):
    """Synthesize candidate FieldMap connecting discovered host fields to Canonical ontology."""
    domain_enum = _entity_type(req.domain)
    discovered = _prepare_synthesis_discovered(domain_enum.value, req)
    vendor = _resolve_vendor(auth.tenant_id, req.vendor)
    mapping, report = synthesizer.synthesize(
        discovered=discovered,
        canonical_type=domain_enum,
        vendor=vendor,
    )
    return _finalize_synthesis(auth, mapping, report, req.sample_records)


@router.post("/v1/schema/synthesize/agentic")
async def synthesize_mapping_agentic(
    req: SynthesizeRequest,
    request: Request,
    auth: AuthContext = Depends(require(Permission.MANAGE_SCHEMA)),
):
    """Synthesize FieldMap with LLM semantic reasoning over ambiguous/vendor host fields."""
    enforce_rate_limit(request, auth)
    domain_enum = _entity_type(req.domain)
    discovered = _prepare_synthesis_discovered(domain_enum.value, req)
    try:  # the app's one gateway: no client, egress store or SQLite connection built per request (#2)
        gateway = request.app.state.supervisor.get_orchestrator().llm_gateway
    except AttributeError:
        raise LLMNotConfigured("no application LLM gateway") from None
    vendor = _resolve_vendor(auth.tenant_id, req.vendor)
    mapping, report = await synthesizer.synthesize_agentic(
        discovered=discovered,
        canonical_type=domain_enum,
        tenant_id=auth.tenant_id,
        llm_gateway=gateway,
        vendor=vendor,
    )
    return _finalize_synthesis(auth, mapping, report, req.sample_records)


@router.get("/v1/schema/mappings")
def list_schema_mappings(auth: AuthContext = Depends(require(Permission.MANAGE_SCHEMA))):
    """List all persisted dynamic entity mappings for this tenant."""
    return {
        "tenant_id": auth.tenant_id,
        "mappings": mapping_store.list_mappings(auth.tenant_id),
        "versions": mapping_store.list_versions(auth.tenant_id),
    }


@router.get("/v1/schema/mappings/{vendor}/{entity_type}")
def get_schema_mapping(
    vendor: str,
    entity_type: str,
    auth: AuthContext = Depends(require(Permission.MANAGE_SCHEMA)),
):
    """Retrieve the exact compiled FieldMap for a given vendor and entity type."""
    try:
        mapping = mapping_store.load_mapping(auth.tenant_id, vendor, entity_type)
    except MappingGovernanceError as exc:  # INVALID_KEY: a client error, never a 500 (E2E-GOV-3)
        raise ApiError(400, {"error": exc.code}) from None
    if not mapping:
        raise HTTPException(status_code=404, detail=f"No mapping found for {vendor}/{entity_type}")
    from hrms_plugin.storage.mapping_store import entity_mapping_to_dict
    return entity_mapping_to_dict(mapping)


@router.post("/v1/schema/fingerprints/approve")
def approve_schema_fingerprint(
    req: FingerprintApproveRequest,
    auth: AuthContext = Depends(
        require(Permission.MANAGE_SCHEMA, max_token_age_seconds=REMEDIATION_MAX_TOKEN_AGE_SECONDS)
    ),
):
    record = payroll_service.approve_fingerprint(auth.tenant_id, req.vendor, req.doctype, req.fingerprint, auth.user_id)
    if "error" in record:
        if record["error"] == "SAME_PERSON":
            raise ApiError(403, {"error": "SAME_PERSON"})
        raise ApiError(400, record)
    res = {"tenant_id": auth.tenant_id, "vendor": req.vendor, "doctype": req.doctype, **record}
    if record.get("status") == "PENDING_CONFIRMATION":
        return JSONResponse(status_code=202, content=res)
    return res


@router.get("/v1/schema/fingerprints/quarantine")
def list_schema_quarantine(auth: AuthContext = Depends(require(Permission.MANAGE_SCHEMA))):
    return {"tenant_id": auth.tenant_id, "quarantine": payroll_service.list_quarantine(auth.tenant_id)}


@router.post("/v1/writeback/proposals")
async def propose_writeback(
    req: WritebackProposalRequest, auth: AuthContext = Depends(require(Permission.MUTATE_EMPLOYEE))
):
    return await writeback_service.propose(auth, req.doctype, req.name, req.patch)


_FRESH_MUTATE = require(Permission.MUTATE_EMPLOYEE, max_token_age_seconds=REMEDIATION_MAX_TOKEN_AGE_SECONDS)


@router.post("/v1/writeback/proposals/{proposal_id}/approve")
async def approve_writeback(proposal_id: str, auth: AuthContext = Depends(_FRESH_MUTATE)):
    return await writeback_service.approve(auth, proposal_id)


@router.post("/v1/writeback/proposals/{proposal_id}/rollback")
async def rollback_writeback(proposal_id: str, auth: AuthContext = Depends(_FRESH_MUTATE)):
    res = await writeback_service.rollback(auth, proposal_id)
    if res.get("status") == "ROLLBACK_PENDING":
        return JSONResponse(status_code=202, content=res)
    return res


class ApproveMappingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # field -> {"data_class": PII|COMPENSATION|INTERNAL|PUBLIC, "llm_visible": bool}; omitted fields count as PII (QD)
    classification: Dict[str, Any] = Field(default_factory=dict)


class ManualFieldConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1)
    transform: Optional[str] = None
    values: Optional[Dict[str, str]] = None


class ReviewMappingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accept: List[str] = Field(default_factory=list)
    reject: List[str] = Field(default_factory=list)
    manual: Dict[str, ManualFieldConfig] = Field(default_factory=dict)


class RejectMappingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1)


@router.post("/v1/schema/mappings/{vendor}/{entity_type}/versions/{version}/approve")
def approve_mapping_version(
    vendor: str,
    entity_type: str,
    version: int,
    req: Optional[ApproveMappingRequest] = None,
    auth: AuthContext = Depends(
        require(Permission.MANAGE_SCHEMA, max_token_age_seconds=REMEDIATION_MAX_TOKEN_AGE_SECONDS)
    ),
):
    """Activate a proposed mapping version; the approver must differ from the proposer (piece G4)."""
    try:
        row = mapping_store.approve_version(auth.tenant_id, vendor, entity_type, version, approver=auth.user_id,
                                           classification=req.classification if req else None)
    except MappingGovernanceError as exc:
        if exc.code == "REVIEW_REQUIRED":
            raise ApiError(409, {"error": "REVIEW_REQUIRED", "fields": exc.fields}) from None
        status = {"NOT_FOUND": 404, "SAME_PERSON": 403, "INVALID_STATE": 409, "PROBE_FAILED": 409,
                  "CLASSIFICATION_UNKNOWN_FIELD": 422, "INVALID_CLASSIFICATION": 422,
                  "SENSITIVE_FIELD_LLM_VISIBLE": 422}.get(exc.code, 400)
        raise ApiError(status, {"error": exc.code}) from None
    return {k: v for k, v in row.items() if k != "mapping"}


@router.post("/v1/schema/mappings/{vendor}/{entity_type}/versions/{version}/review")
def review_mapping_version(
    vendor: str,
    entity_type: str,
    version: int,
    req: ReviewMappingRequest,
    auth: AuthContext = Depends(
        require(Permission.MANAGE_SCHEMA, max_token_age_seconds=REMEDIATION_MAX_TOKEN_AGE_SECONDS)
    ),
):
    """Review and revise a proposed schema mapping; creates a new PROPOSED version and marks old REVISED."""
    try:
        actions = {
            "accept": req.accept,
            "reject": req.reject,
            "manual": {k: v.model_dump() for k, v in req.manual.items()},
        }
        row = mapping_store.review_version(
            auth.tenant_id,
            vendor,
            entity_type,
            version,
            reviewer=auth.user_id,
            actions=actions,
        )
    except MappingGovernanceError as exc:
        if exc.code == "REVIEW_INVALID":
            raise ApiError(422, {"error": "REVIEW_INVALID", "problems": exc.problems}) from None
        status = {"NOT_FOUND": 404, "INVALID_STATE": 409}.get(exc.code, 400)
        raise ApiError(status, {"error": exc.code}) from None
    return {k: v for k, v in row.items() if k != "mapping"}


@router.post("/v1/schema/mappings/{vendor}/{entity_type}/versions/{version}/reject")
def reject_mapping_version(
    vendor: str,
    entity_type: str,
    version: int,
    req: RejectMappingRequest,
    auth: AuthContext = Depends(
        require(Permission.MANAGE_SCHEMA, max_token_age_seconds=REMEDIATION_MAX_TOKEN_AGE_SECONDS)
    ),
):
    """Reject a proposed schema mapping version."""
    try:
        row = mapping_store.reject_version(
            auth.tenant_id,
            vendor,
            entity_type,
            version,
            rejected_by=auth.user_id,
            reason=req.reason,
        )
    except MappingGovernanceError as exc:
        status = {"NOT_FOUND": 404, "INVALID_STATE": 409}.get(exc.code, 400)
        raise ApiError(status, {"error": exc.code}) from None
    return {k: v for k, v in row.items() if k != "mapping"}


class PfCalculateRequest(BaseModel):
    """IDs only: tenant comes from the token and PF is Indian payroll, so neither is a client field (piece H6)."""

    model_config = ConfigDict(extra="forbid")

    employee_id: StrictStr
    period: StrictStr


_PF_ERROR_STATUS = {
    "INVALID_ARGUMENT": 422,
    "NOT_APPLICABLE": 422,
    "SLIP_NOT_FOUND": 404,
    "HOST_NOT_CONFIGURED": 409,
    "VENDOR_NOT_SUPPORTED": 409,
    "RULES_NOT_CONFIGURED": 409,
    "MAPPING_NOT_APPROVED": 409,
    "SCHEMA_DRIFT": 409,
    "AMBIGUOUS_SLIPS": 409,
    "PF_COMPONENT_MISSING": 409,
}


@router.post("/v1/payroll/pf/calculate")
async def calculate_pf(req: PfCalculateRequest, auth: AuthContext = Depends(require(Permission.VIEW_SALARY))):
    """Deterministic EPF/EPS calculation without any LLM call (piece H6, Q4)."""
    result = await payroll_service.calculate_pf(auth.tenant_id, req.employee_id, req.period, "IN")
    if "error" in result:
        raise ApiError(_PF_ERROR_STATUS.get(result["error"], 502), result)
    return result


@router.get("/v1/payroll/pf/results/{employee_id}/{period}")
def get_pf_result(employee_id: str, period: str, auth: AuthContext = Depends(require(Permission.VIEW_SALARY))):
    """A stored PF result with its provenance (QB): what was calculated, from which document, under which rules."""
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", period):
        raise ApiError(422, {"error": "INVALID_ARGUMENT", "detail": "period must be YYYY-MM"})
    stored = payroll_service.get_result(auth.tenant_id, employee_id, period)
    if stored is None:
        raise ApiError(404, {"error": "NOT_FOUND"})
    return stored


_CONNECTOR_ERROR_STATUS = {"HOST_NOT_CONFIGURED": 409, "ENTITY_NOT_SUPPORTED": 409, "VENDOR_MISMATCH": 500}


@router.get("/v1/hosts/requisitions")
async def list_host_requisitions(auth: AuthContext = Depends(require(Permission.MANAGE_RECRUITMENT))):
    """Requisitions from this tenant's host HRMS in canonical form, via its declarative profile (piece I7)."""
    from hrms_plugin.integrations.declarative import ConnectorError, DeclarativeConnector, ProfileError, load_profile
    from hrms_plugin.integrations.host_registry import HostRegistryError, load_host_registry_from_env
    from hrms_plugin.integrations.mapping_overlay import OverlayError, overlay

    try:
        host = load_host_registry_from_env().for_tenant(auth.tenant_id)
    except HostRegistryError:
        host = None
    if host is None:
        raise ApiError(409, {"error": "HOST_NOT_CONFIGURED"})
    try:
        profile = load_profile(host.vendor)
    except ProfileError:
        raise ApiError(500, {"error": "PROFILE_INVALID"}) from None
    # Q1 resolution order: the tenant's approved mapping overrides the vendor profile; proposals never apply (QC).
    layer: Dict[str, Any] = {"layer": "VENDOR_PROFILE"}
    active = mapping_store.active_version(auth.tenant_id, host.vendor, "REQUISITION")
    if active is not None and "REQUISITION" in profile.entities:
        version, approved = active
        try:
            spec, overridden = overlay(profile.entities["REQUISITION"], approved)
        except OverlayError as exc:
            raise ApiError(409, {"error": "MAPPING_NOT_APPLICABLE", "version": version, "field": exc.field,
                                 "detail": exc.reason}) from None
        profile = profile.model_copy(update={"entities": {**profile.entities, "REQUISITION": spec}})
        layer = {"layer": "TENANT_OVERRIDE", "version": version, "fields": overridden}
    try:
        result = await DeclarativeConnector(host, profile).list_entity("REQUISITION")
    except ConnectorError as exc:
        raise ApiError(_CONNECTOR_ERROR_STATUS.get(exc.code, 502), {"error": exc.code}) from None
    schema = _check_requisition_shape(auth.tenant_id, host.vendor, profile.entities["REQUISITION"], result.rows)
    return {"vendor": host.vendor, "requisitions": result.records, "rejected": result.rejected, "mapping": layer,
            "schema": schema}


def _shape_key(tenant_id: str, vendor: str) -> str:
    return f"{tenant_id}:{vendor}:REQUISITION"


def _check_requisition_shape(tenant_id: str, vendor: str, spec: Any, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Q2 for host reads (QE): compare the payload shape with the approved one; drift is quarantined, not mapped."""
    from hrms_plugin.integrations.declarative import observed_shape, shape_changes, shape_fingerprint

    if not rows:
        return {"status": "NO_ROWS"}
    key = _shape_key(tenant_id, vendor)
    shape = observed_shape(rows, spec)
    fingerprint = shape_fingerprint(shape)
    approved = _storage.get("approved_shapes", key)
    if approved is None:
        _storage.set("observed_shapes", key, {"fingerprint": fingerprint, "shape": shape})
        return {"status": "UNAPPROVED", "fingerprint": fingerprint}
    changed = shape_changes(approved["shape"], shape)
    if changed:
        _storage.set("quarantine", f"{key}:{fingerprint}", {"vendor": vendor, "entity": "REQUISITION",
                                                            "observed_fingerprint": fingerprint, "changed": changed,
                                                            "quarantined_at": time.time()})
        logger.warning("requisition schema drift", extra={"reason": "SCHEMA_DRIFT", "tenant": tenant_id})
        raise ApiError(409, {"error": "SCHEMA_DRIFT", "changed": changed, "observed_fingerprint": fingerprint})
    return {"status": "APPROVED", "fingerprint": approved["fingerprint"]}


class ShapeApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


@router.post("/v1/hosts/requisitions/shape/approve")
def approve_requisition_shape(
    req: ShapeApproveRequest,
    request: Request,
    auth: AuthContext = Depends(require(Permission.MANAGE_SCHEMA)),
):
    """Approve the requisition payload shape last observed for this tenant's host (QE)."""
    from hrms_plugin.integrations.host_registry import HostRegistryError, load_host_registry_from_env

    clock = (
        getattr(request.app.state, "clock", time.time)
        if hasattr(request, "app") and hasattr(request.app, "state")
        else time.time
    )
    now = clock()

    try:
        host = load_host_registry_from_env().for_tenant(auth.tenant_id)
    except HostRegistryError:
        host = None
    if host is None:
        raise ApiError(409, {"error": "HOST_NOT_CONFIGURED"})
    key = _shape_key(auth.tenant_id, host.vendor)
    observed = _storage.get("observed_shapes", key)
    if observed is None or observed["fingerprint"] != req.fingerprint:
        raise ApiError(409, {"error": "FINGERPRINT_NOT_OBSERVED"})

    pending = _storage.get("pending_shapes", key)
    requested_at = pending.get("requested_at") if pending else None
    is_expired = requested_at is not None and (now > requested_at + 24 * 3600)
    if not pending or pending.get("fingerprint") != req.fingerprint or is_expired:
        pending_record = {
            "fingerprint": req.fingerprint,
            "requested_by": auth.user_id,
            "requested_at": now,
        }
        _storage.set("pending_shapes", key, pending_record)
        return JSONResponse(status_code=202, content={"status": "PENDING_CONFIRMATION", **pending_record})

    if pending.get("requested_by") == auth.user_id:
        raise ApiError(403, {"error": "SAME_PERSON"})

    record = {
        **observed,
        "approved_by": auth.user_id,
        "approved_at": now,
        "requested_by": pending["requested_by"],
        "requested_at": pending.get("requested_at"),
    }
    _storage.set("approved_shapes", key, record)
    _storage.delete("pending_shapes", key)
    return {"status": "APPROVED", "fingerprint": req.fingerprint}


@router.get("/v1/governance/llm-egress")
def list_llm_egress(auth: AuthContext = Depends(require(Permission.VIEW_COMPLIANCE))):
    """Content-free record of every LLM call made for this tenant (piece G1)."""
    return {"tenant_id": auth.tenant_id, "records": egress_log.list(auth.tenant_id)}


@router.delete("/v1/governance/llm-egress")
def delete_llm_egress(
    auth: AuthContext = Depends(
        require(Permission.MANAGE_GOVERNANCE, max_token_age_seconds=REMEDIATION_MAX_TOKEN_AGE_SECONDS)
    ),
):
    """Deletion on request (QF): every egress row of the caller's tenant. Super admin, freshly authenticated."""
    return {"tenant_id": auth.tenant_id, "deleted": egress_log.delete_tenant(auth.tenant_id)}


@router.post("/v1/governance/disclosures/lookup")
def lookup_disclosure(
    req: DisclosureLookupRequest,
    request: Request,
    auth: AuthContext = Depends(
        require(Permission.MANAGE_GOVERNANCE, max_token_age_seconds=REMEDIATION_MAX_TOKEN_AGE_SECONDS)
    ),
):
    """Lookup disclosures of a specific PII item for this tenant (K2.4)."""
    enforce_rate_limit(request, auth)
    if not disclosure.is_configured():
        raise ApiError(503, {"error": "DISCLOSURE_INDEX_NOT_CONFIGURED"})

    current_log = getattr(request.app.state, "egress_log", None) or egress_log
    ref = disclosure.compute_ref(auth.tenant_id, req.kind, req.value)
    disclosures = current_log.find_subject(auth.tenant_id, ref, limit=200) if ref else []
    found = bool(disclosures)
    current_log.record_lookup(auth.tenant_id, auth.user_id, req.kind, found)

    return {
        "kind": req.kind,
        "found": found,
        "disclosures": disclosures,
    }


@router.post("/v1/remediate/execute")
def execute_remediation(
    req: RemediationExecuteRequest,
    auth: AuthContext = Depends(
        require(Permission.EXECUTE_REMEDIATION, max_token_age_seconds=REMEDIATION_MAX_TOKEN_AGE_SECONDS)
    ),
):
    """Issue host audit attribution headers for a reviewed remediation. This endpoint writes NOTHING to the host:
    changes are made only through /v1/writeback/proposals, approved by a different person (piece F2, agy D-F2)."""
    if not audit_gateway.is_configured():
        raise AuditSigningError("AUDIT_SIGNING_NOT_CONFIGURED")
    # Generate Host Audit Attribution Headers
    headers = audit_gateway.create_attribution_headers(
        tenant_id=auth.tenant_id,
        agent_name="StatutoryComplianceAuditor",
        hitl_approver_id=auth.user_id,
        reasoning_payload={
            "violation_id": req.violation_id,
            "entity": req.target_entity,
            "entity_id": req.entity_id,
            "patch": req.patch_payload,
        },
    )

    return {
        "status": "ATTRIBUTION_ONLY",
        "applied": False,
        "violation_id": req.violation_id,
        "entity_id": req.entity_id,
        "patch_applied": req.patch_payload,
        "audit_tracing": headers.to_http_headers(),
    }


@router.get("/v1/compliance/diagnose")
@router.get("/api/compliance/agent/diagnose")
def get_compliance_diagnosis(
    request: Request,
    jurisdiction: str = "AE",
    auth: AuthContext = Depends(require(Permission.VIEW_COMPLIANCE)),
):
    """Diagnose employee records against statutory requirements. No host employee feed is connected yet, so this
    answers 503 unless HRMS_DEMO_ENDPOINTS=true, in which case it audits labelled sample records (P1)."""
    _require_demo(request)
    if jurisdiction not in STATUTORY_JURISDICTIONS:
        raise ApiError(422, {"error": "JURISDICTION_NOT_SUPPORTED", "supported": STATUTORY_JURISDICTIONS})
    sample_employees = [
        {
            "id": "EMP-101",
            "name": "Rashid Al-Falasi",
            "basic_salary": 4500.0,
            "gross_salary": 12000.0,
            "probation_days": 210,
            "weekly_hours": 52,
            "uae_wps_registered": False,
        },
        {
            "id": "EMP-102",
            "name": "Sunita Verma",
            "basic_salary": 14000.0,
            "gross_salary": 28000.0,
            "uan_number": "",
            "esic_number": "",
        },
    ]

    from hrms_plugin.rag.store import Jurisdiction

    jur_enum = Jurisdiction(jurisdiction)
    audit_res = compliance_agent.audit_employees(
        tenant_id=auth.tenant_id,
        jurisdiction=jur_enum,
        employees=sample_employees,
    )

    action_items = []
    for v in audit_res.violations:
        action_items.append(
            {
                "id": v.violation_id,
                "severity": v.severity,
                "title": f"Statutory Violation: {v.summary}",
                "description": v.detailed_findings,
                "affected_count": 1,
                "action_type": "APPLY_STATUTORY_REMEDIATION",
                "statutory_ref": v.statutory_citation,
                "jurisdiction": jur_enum.value,
                "requires_human_approval": True,
                "suggested_fix": {
                    "label": "1-Click Auto Fix",
                    "target_endpoint": "/api/employees/update",
                    "method": "PATCH",
                },
                "remediation_patch": {
                    "action_type": "UPDATE_RECORD",
                    "entity_id": v.entity_id,
                    "target_endpoint": "/api/employees/update",
                    "http_method": "PATCH",
                    "diff_preview": {
                        "statutory_alignment": {"from": "NON_COMPLIANT", "to": "RESOLVED"},
                    },
                    "direct_payload": v.remediation_patch or {},
                    "remediation_summary": f"Align records with {v.statutory_citation}",
                },
            }
        )

    score = 92 if audit_res.is_compliant else max(50, 100 - (len(audit_res.violations) * 15))

    return {
        "success": True,
        "overall_score": score,
        "total_actions": len(action_items),
        "action_items": action_items,
        "jurisdiction": jur_enum.value,
        "data_source": "DEMO_SAMPLE",
    }


@router.get("/v1/compliance/dashboard/live")
@router.get("/api/compliance/dashboard/live")
def get_compliance_dashboard_live(request: Request, auth: AuthContext = Depends(require(Permission.VIEW_COMPLIANCE))):
    """Statutory setup matrix. Nothing here is verified against a host yet, so it answers 503 unless
    HRMS_DEMO_ENDPOINTS=true, in which case the illustrative matrix is returned labelled (P2)."""
    _require_demo(request)
    return {
        "success": True,
        "data_source": "DEMO_SAMPLE",
        "setup_items": [
            {"label": "MOHRE Establishment Registered", "status": "compliant", "icon": "Building2"},
            {"label": "WPS Registered & Active", "status": "compliant", "icon": "Banknote"},
            {"label": "Wage Protection SIF Verified", "status": "compliant", "icon": "Wallet"},
            {"label": "ILOE Insurance Configured", "status": "compliant", "icon": "HeartPulse"},
            {"label": "EPFO & ESIC Challan Sync", "status": "compliant", "icon": "CalendarDays"},
            {"label": "Gratuity (UAE Art. 51 / India 1972)", "status": "compliant", "icon": "Coins"},
        ],
    }


class LegacyChatRequest(BaseModel):
    message: StrictStr
    jurisdiction: JurisdictionCode  # from the request, never guessed from keywords (P9)
    context: Dict[str, Any] = Field(default_factory=dict)


@router.post("/api/compliance/agent/chat")
def legacy_chat_adapter(
    req: LegacyChatRequest,
    request: Request,
    auth: AuthContext = Depends(require(Permission.USE_COPILOT)),
):
    """Legacy endpoint adapter for Next.js ComplianceCopilotSidecar."""
    enforce_rate_limit(request, auth)
    check_message(req.message)
    resp = request.app.state.supervisor.process_message(
        message=req.message,
        jurisdiction=req.jurisdiction,
        context=req.context,
        auth=auth,
    )
    return {
        "success": True,
        "reply": resp.reply_text,
        "statutory_citations": resp.statutory_citations,
        "tool_calls": [{"tool": resp.routed_agent, "args": {"intent": resp.intent.value}}],
    }


class GenerateArchetypesRequest(BaseModel):
    prompt: str
    location: Optional[str] = "Remote"
    options: Optional[Dict[str, Any]] = None


@router.post("/api/requisitions/generate-archetypes")
@router.post("/v1/requisitions/generate-archetypes")
def generate_requisition_archetypes(
    req: GenerateArchetypesRequest,
    auth: AuthContext = Depends(require(Permission.MANAGE_RECRUITMENT)),
):
    """Generate multi-archetype job requisition proposals for Next.js SmartRequisitionArchetypeSelector."""
    p = req.prompt.strip()
    loc = req.location or "Hybrid / Flexible"

    # Extract keywords
    words = [w.capitalize() for w in p.split() if len(w) > 2]
    main_title = p.title() if p else "Software Engineer"
    skills = [w for w in words if w.lower() not in ("senior", "junior", "lead", "engineer", "manager", "developer")]
    if not skills:
        skills = ["Python", "FastAPI", "React", "PostgreSQL", "System Design"]

    archetypes = [
        {
            "id": "arch-specialist",
            "name": f"Domain Specialist ({main_title})",
            "badge": "High Depth & Velocity",
            "tagline": "Deep specialized technical mastery and independent problem-solving.",
            "summary": (
                f"Engineered for high-impact ownership of {main_title} initiatives. "
                "Focuses on architectural integrity, clean execution, and technical excellence."
            ),
            "skills_display": skills[:4] + ["Architecture", "Problem Solving"],
            "benefits_display": [],
            "jobData": {
                "title": main_title,
                "department": "Engineering",
                "type": "Full Time",
                "jobMode": "Hybrid",
                "location": loc,
                "shifts": "General Shift (9 AM - 6 PM)",
                "description": f"Seeking a passionate {main_title} to build scalable, mission-critical systems.",
                "experience": "4-7 Years",
                "education": "Bachelor's or Master's in Computer Science or equivalent",
                "salary": None,
                "minSalary": None,
                "maxSalary": None,
                "validTill": None,
                "position": 1,
                "minthreshold": 70,
                "matchthreshold": 85,
                "jobDescription": f"As a {main_title}, you will design, implement, and maintain scalable solutions.",
                "requirements": f"Proven expertise in {', '.join(skills[:3])}. Strong architectural problem-solving.",
                "responsibilities": (
                    "Drive technical roadmap and feature delivery. Collaborate cross-functionally with product teams."
                ),
            },
        },
        {
            "id": "arch-lead",
            "name": f"Team Lead / Staff ({main_title})",
            "badge": "Strategic Leadership",
            "tagline": "Drives engineering standards, mentorship, and cross-functional alignment.",
            "summary": (
                f"Combines technical depth in {main_title} with squad leadership and mentorship capabilities."
            ),
            "skills_display": skills[:3] + ["Team Leadership", "Code Review", "Sprint Planning"],
            "benefits_display": [],
            "jobData": {
                "title": f"Lead {main_title}",
                "department": "Engineering",
                "type": "Full Time",
                "jobMode": "Hybrid",
                "location": loc,
                "shifts": "General Shift",
                "description": f"Lead and inspire a high-performing squad delivering on {main_title} priorities.",
                "experience": "7-10 Years",
                "education": "Bachelor's Degree in relevant discipline",
                "salary": None,
                "minSalary": None,
                "maxSalary": None,
                "validTill": None,
                "position": 1,
                "minthreshold": 75,
                "matchthreshold": 90,
                "jobDescription": f"Lead the development and engineering practices for our {main_title} squad.",
                "requirements": "Demonstrated track record of technical leadership and team development.",
                "responsibilities": "Mentor engineers, review pull requests, and ensure delivery SLA compliance.",
            },
        },
        {
            "id": "arch-fast-builder",
            "name": f"Agile Builder ({main_title})",
            "badge": "Rapid Prototyping",
            "tagline": "Pragmatic, high-velocity contributor who ships early and iterates fast.",
            "summary": "Tailored for early-stage feature discovery, MVP shipping, and customer feedback loops.",
            "skills_display": skills[:3] + ["Rapid Iteration", "Full Stack", "Git"],
            "benefits_display": [],
            "jobData": {
                "title": main_title,
                "department": "Product Engineering",
                "type": "Full Time",
                "jobMode": "Remote",
                "location": loc,
                "shifts": "Flexible Hours",
                "description": f"Ship customer-facing features at high speed as a {main_title}.",
                "experience": "2-5 Years",
                "education": "Relevant degree or demonstrated open-source portfolio",
                "salary": None,
                "minSalary": None,
                "maxSalary": None,
                "validTill": None,
                "position": 2,
                "minthreshold": 60,
                "matchthreshold": 75,
                "jobDescription": f"Hands-on execution and high-tempo delivery across {main_title} features.",
                "requirements": "Strong bias for action and ability to write maintainable code quickly.",
                "responsibilities": "Build, test, and deploy features in bi-weekly sprint cycles.",
            },
        },
    ]

    return {
        "success": True,
        "prompt": p,
        "source": "TEMPLATE",  # not generated; pay, validity and benefits are left for the recruiter (P8)
        "requires_review": True,
        "archetypes": archetypes,
    }


async def _api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.body, headers=exc.headers)


async def _voice_upstream_error_handler(request: Request, exc: VoiceUpstreamError) -> JSONResponse:
    status = {"VOICE_UPSTREAM_TIMEOUT": 504, "VOICE_NOT_CONFIGURED": 503}.get(exc.code, 502)
    return JSONResponse(status_code=status, content={"error": exc.code})


async def _writeback_error_handler(_request: Request, exc: WritebackError) -> JSONResponse:
    status = WRITEBACK_STATUS.get(exc.code, 500)
    return JSONResponse(status_code=status, content={"error": exc.code, "detail": exc.detail})


async def _audit_signing_error_handler(_request: Request, _exc: AuditSigningError) -> JSONResponse:
    return JSONResponse(status_code=503, content={"error": "AUDIT_SIGNING_NOT_CONFIGURED"})


async def _llm_not_allowed_handler(_request: Request, _exc: LlmNotAllowed) -> JSONResponse:
    return JSONResponse(status_code=403, content={"error": _exc.code})


async def _llm_budget_exceeded_handler(_request: Request, _exc: LlmBudgetExceeded) -> JSONResponse:
    return JSONResponse(status_code=429, content={"error": "LLM_BUDGET_EXHAUSTED"})


async def _llm_not_configured_handler(_request: Request, _exc: LLMNotConfigured) -> JSONResponse:
    return JSONResponse(status_code=503, content={"error": "LLM_NOT_CONFIGURED"})


async def _pii_egress_blocked_handler(_request: Request, exc: PiiEgressBlocked) -> JSONResponse:
    return JSONResponse(status_code=422, content={"error": "PII_EGRESS_BLOCKED", "kinds": exc.kinds})


async def _permission_denied_handler(_request: Request, exc: PermissionDenied) -> JSONResponse:
    return JSONResponse(status_code=403, content={"error": "FORBIDDEN", "permission": exc.permission.value})


def create_app(
    *,
    registry: TenantRegistry,
    jwks_cache: JwksCache,
    clock: Callable[[], float] = time.time,
    supervisor_agent: Optional[SupervisorAgent] = None,
    env: Optional[Mapping[str, str]] = None,
) -> FastAPI:
    env = os.environ if env is None else env
    docs = env.get(API_DOCS_ENV_VAR) == "true"
    application = FastAPI(
        title="Agentic HRMS AI Plugin Service",
        version="2.0.0",
        description="Universal, host-agnostic AI copilot and statutory compliance sidecar for HRMS suites.",
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    application.state.token_verifier = TokenVerifier(registry, jwks_cache, clock=clock)
    application.state.registry = registry
    application.state.rate_limiter = TenantRateLimiter(_policy_limit(registry))
    application.state.demo_endpoints = env.get("HRMS_DEMO_ENDPOINTS") == "true"
    application.state.clock = clock
    application.state.supervisor = supervisor_agent or supervisor
    application.state.egress_log = egress_log
    application.add_middleware(
        CORSMiddleware,
        allow_origins=registry.all_origins(),
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Tenant-Id"],
    )
    application.add_exception_handler(ApiError, _api_error_handler)
    application.add_exception_handler(PermissionDenied, _permission_denied_handler)
    application.add_exception_handler(PiiEgressBlocked, _pii_egress_blocked_handler)
    application.add_exception_handler(LLMNotConfigured, _llm_not_configured_handler)
    application.add_exception_handler(LlmNotAllowed, _llm_not_allowed_handler)
    application.add_exception_handler(LlmBudgetExceeded, _llm_budget_exceeded_handler)
    application.add_exception_handler(WritebackError, _writeback_error_handler)
    application.add_exception_handler(AuditSigningError, _audit_signing_error_handler)
    application.add_exception_handler(VoiceUpstreamError, _voice_upstream_error_handler)
    application.include_router(router)
    application.include_router(mcp_router)
    return application


def _create_default_app() -> FastAPI:
    return create_app(
        registry=load_registry_from_env(),
        jwks_cache=JwksCache(make_httpx_fetcher(httpx.AsyncClient())),
    )


_default_app: Optional[FastAPI] = None


def __getattr__(name: str) -> Any:
    """Build the default app on first access to `app` (uvicorn, `python -m hrms_plugin.server`).

    Importing `create_app` must not require a tenant registry; starting the service still fails fast without one.
    """
    global _default_app
    if name == "app":
        if _default_app is None:
            _default_app = _create_default_app()
        return _default_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
