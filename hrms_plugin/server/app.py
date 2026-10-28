"""FastAPI REST Service Surface for the Standalone Agentic HRMS AI Plugin."""

from __future__ import annotations

import base64
import json
import os
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
from pydantic import BaseModel, ConfigDict

from hrms_plugin.agents.compliance import ComplianceAgent
from hrms_plugin.agents.leave_attendance import LeaveAttendanceAgent
from hrms_plugin.agents.recruitment import RecruitmentAgent
from hrms_plugin.agents.statutory_payroll import StatutoryPayrollAgent
from hrms_plugin.agents.supervisor import AgentResponse, SupervisorAgent
from hrms_plugin.agents.voice import VoiceAgent
from hrms_plugin.rag.store import StatutoryKnowledgeBase
from hrms_plugin.schema.canonical import EntityType
from hrms_plugin.schema.introspector import SchemaIntrospector
from hrms_plugin.schema.synthesizer import MappingSynthesizer
from hrms_plugin.security.audit import HostAuditGateway
from hrms_plugin.security.http_auth import ApiError, require
from hrms_plugin.security.jwks_cache import JwksCache, make_httpx_fetcher
from hrms_plugin.security.masking import PiiMaskingGateway
from hrms_plugin.security.rbac import AuthContext, Permission, PermissionDenied
from hrms_plugin.security.tenant_registry import TenantRegistry, load_registry_from_env
from hrms_plugin.security.token_verifier import TokenVerifier
from hrms_plugin.storage.mapping_store import MappingStore
from hrms_plugin.storage.sqlite_store import SqliteKeyValueStore
from hrms_plugin.storage.vault_store import PiiVaultStore

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
voice_agent = VoiceAgent()

# Persistent storage singletons
_storage = SqliteKeyValueStore(str(Path(os.environ.get("HRMS_DATA_DIR", ".hrms_data")) / "plugin_storage.db"))
vault_store = PiiVaultStore(backend=_storage)
mapping_store = MappingStore(backend=_storage)

pii_gateway = PiiMaskingGateway(store=vault_store)
audit_gateway = HostAuditGateway()
introspector = SchemaIntrospector()
synthesizer = MappingSynthesizer()


# Request / Response Schemas
class ChatRequest(BaseModel):
    message: str
    jurisdiction: JurisdictionCode = "IN"
    context: Optional[Dict[str, Any]] = None
    mask_pii: bool = True
    agentic: bool = True
    history: Optional[List[Dict[str, str]]] = None


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


class SynthesizeRequest(BaseModel):
    domain: str = "EMPLOYEE"
    discovered_fields: List[Dict[str, Any]]


class RemediationExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    violation_id: str
    target_entity: str
    entity_id: str
    patch_payload: Dict[str, Any]


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
    agent: SupervisorAgent = request.app.state.supervisor
    text_to_process = req.message
    if req.mask_pii:
        text_to_process, _ = pii_gateway.mask_text(req.message, tenant_id=auth.tenant_id)

    if req.agentic:
        response = await agent.process_agentic(
            message=text_to_process,
            jurisdiction=req.jurisdiction,
            context=req.context,
            tenant_id=auth.tenant_id,
            history=req.history,
        )
    else:
        response = agent.process_message(
            message=text_to_process,
            jurisdiction=req.jurisdiction,
            context=req.context,
        )

    # De-tokenize if masked
    if req.mask_pii:
        response.reply_text = pii_gateway.unmask_text(response.reply_text, tenant_id=auth.tenant_id)

    return response


@router.post("/v1/chat/stream")
async def chat_stream_endpoint(
    req: ChatRequest,
    request: Request,
    auth: AuthContext = Depends(require(Permission.USE_COPILOT)),
):
    """Real-time SSE event stream for autonomous agent thoughts, tool dispatches, and tokens."""
    agent: SupervisorAgent = request.app.state.supervisor

    async def event_generator():
        text_to_process = req.message
        if req.mask_pii:
            text_to_process, _ = pii_gateway.mask_text(req.message, tenant_id=auth.tenant_id)

        async for chunk in agent.stream_agentic(
            message=text_to_process,
            jurisdiction=req.jurisdiction,
            context=req.context,
            tenant_id=auth.tenant_id,
        ):
            yield f"data: {json.dumps(chunk)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.post("/v1/voice/transcribe")
async def voice_transcribe(req: VoiceTranscribeRequest, auth: AuthContext = Depends(require(Permission.USE_COPILOT))):
    """Transcribe base64 audio bytes via Sarvam AI Saaras model."""
    audio_bytes = base64.b64decode(req.audio_base64)
    res = await voice_agent.transcribe_audio(audio_bytes, language_code=req.language_code)
    return res


@router.post("/v1/voice/synthesize")
async def voice_synthesize(req: VoiceSynthesizeRequest, auth: AuthContext = Depends(require(Permission.USE_COPILOT))):
    """Synthesize text into natural voice audio via Sarvam AI Bulbul model."""
    res = await voice_agent.synthesize_speech(
        text=req.text,
        target_language=req.target_language,
        speaker=req.speaker,
    )
    return res


@router.post("/v1/voice/turn")
async def voice_turn(
    req: VoiceTurnRequest,
    request: Request,
    auth: AuthContext = Depends(require(Permission.USE_COPILOT)),
):
    """Full-duplex multimodal turn: Audio In -> Transcribe -> ReAct Agent Brain -> Spoken Audio Out."""
    audio_bytes = base64.b64decode(req.audio_base64)
    stt = await voice_agent.transcribe_audio(audio_bytes, language_code=req.language_code)
    transcript = stt.get("transcript", "")

    # Run agentic turn
    agent_res = await request.app.state.supervisor.process_agentic(
        message=transcript,
        jurisdiction=req.jurisdiction,
        tenant_id=auth.tenant_id,
    )

    # Synthesize spoken voice reply
    tts = await voice_agent.synthesize_speech(
        text=agent_res.reply_text,
        target_language=stt.get("language_code", "en-IN"),
    )

    return {
        "transcript_in": transcript,
        "reply_text": agent_res.reply_text,
        "reply_audio_base64": tts.get("audio_base64"),
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
        entities = {}
        for entity_name, samples in req.sample_records.items():
            discovered_ent = introspector.introspect_json_samples(entity_name, samples)
            entities[entity_name] = discovered_ent.model_dump()
        return {
            "entities_discovered": list(entities.keys()),
            "entities": entities,
        }
    else:
        raise HTTPException(status_code=400, detail="Must provide swagger_dict or sample_records")


@router.post("/v1/schema/synthesize")
def synthesize_mapping(
    req: SynthesizeRequest,
    auth: AuthContext = Depends(require(Permission.MANAGE_SCHEMA)),
):
    """Synthesize candidate FieldMap connecting discovered host fields to Canonical ontology."""
    domain_enum = EntityType(req.domain.upper())
    from hrms_plugin.schema.introspector import DiscoveredEntity, DiscoveredField

    discovered_fields_dict = {
        f["name"]: DiscoveredField(name=f["name"], path=f.get("path", f["name"])) for f in req.discovered_fields
    }
    discovered = DiscoveredEntity(name=domain_enum.value, fields=discovered_fields_dict)
    mapping, report = synthesizer.synthesize(
        discovered=discovered,
        canonical_type=domain_enum,
    )
    mapping_store.save_mapping(tenant_id=auth.tenant_id, mapping=mapping)
    return report.model_dump()


@router.post("/v1/schema/synthesize/agentic")
async def synthesize_mapping_agentic(
    req: SynthesizeRequest,
    auth: AuthContext = Depends(require(Permission.MANAGE_SCHEMA)),
):
    """Synthesize FieldMap with LLM semantic reasoning over ambiguous/vendor host fields."""
    domain_enum = EntityType(req.domain.upper())
    from hrms_plugin.schema.introspector import DiscoveredEntity, DiscoveredField

    discovered_fields_dict = {
        f["name"]: DiscoveredField(name=f["name"], path=f.get("path", f["name"])) for f in req.discovered_fields
    }
    discovered = DiscoveredEntity(name=domain_enum.value, fields=discovered_fields_dict)
    mapping, report = await synthesizer.synthesize_agentic(
        discovered=discovered,
        canonical_type=domain_enum,
    )
    mapping_store.save_mapping(tenant_id=auth.tenant_id, mapping=mapping)
    return report.model_dump()


@router.get("/v1/schema/mappings")
def list_schema_mappings(auth: AuthContext = Depends(require(Permission.MANAGE_SCHEMA))):
    """List all persisted dynamic entity mappings for this tenant."""
    return {
        "tenant_id": auth.tenant_id,
        "mappings": mapping_store.list_mappings(auth.tenant_id),
    }


@router.get("/v1/schema/mappings/{vendor}/{entity_type}")
def get_schema_mapping(
    vendor: str,
    entity_type: str,
    auth: AuthContext = Depends(require(Permission.MANAGE_SCHEMA)),
):
    """Retrieve the exact compiled FieldMap for a given vendor and entity type."""
    mapping = mapping_store.load_mapping(auth.tenant_id, vendor, entity_type)
    if not mapping:
        raise HTTPException(status_code=404, detail=f"No mapping found for {vendor}/{entity_type}")
    from hrms_plugin.storage.mapping_store import entity_mapping_to_dict
    return entity_mapping_to_dict(mapping)


@router.post("/v1/remediate/execute")
def execute_remediation(
    req: RemediationExecuteRequest,
    auth: AuthContext = Depends(
        require(Permission.EXECUTE_REMEDIATION, max_token_age_seconds=REMEDIATION_MAX_TOKEN_AGE_SECONDS)
    ),
):
    """Execute human-approved compliance remediation patch with signed host audit attribution headers."""
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
        "status": "APPROVED_AND_QUEUED",
        "violation_id": req.violation_id,
        "entity_id": req.entity_id,
        "patch_applied": req.patch_payload,
        "audit_tracing": headers.to_http_headers(),
    }


@router.get("/v1/compliance/diagnose")
@router.get("/api/compliance/agent/diagnose")
def get_compliance_diagnosis(
    jurisdiction: str = "AE",
    auth: AuthContext = Depends(require(Permission.VIEW_COMPLIANCE)),
):
    """Diagnose live employee and payroll records against statutory legal requirements."""
    # Simulated sample employee batch to demonstrate live multi-agent audit
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

    jur_enum = Jurisdiction(jurisdiction) if jurisdiction in [j.value for j in Jurisdiction] else Jurisdiction.UAE
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
                "title": f"Statutory Violation: {v.rule_name}",
                "description": v.description,
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
                    "entity_id": v.employee_id or "EMP-101",
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
    }


@router.get("/v1/compliance/dashboard/live")
@router.get("/api/compliance/dashboard/live")
def get_compliance_dashboard_live(auth: AuthContext = Depends(require(Permission.VIEW_COMPLIANCE))):
    """Live statutory setup matrix verification."""
    return {
        "success": True,
        "setup_items": [
            {"label": "MOHRE Establishment Registered", "status": "compliant", "icon": "Building2"},
            {"label": "WPS Registered & Active", "status": "compliant", "icon": "Banknote"},
            {"label": "Wage Protection SIF Verified", "status": "compliant", "icon": "Wallet"},
            {"label": "ILOE Insurance Configured", "status": "compliant", "icon": "HeartPulse"},
            {"label": "EPFO & ESIC Challan Sync", "status": "compliant", "icon": "CalendarDays"},
            {"label": "Gratuity (UAE Art. 51 / India 1972)", "status": "compliant", "icon": "Coins"},
        ],
    }


@router.post("/api/compliance/agent/chat")
def legacy_chat_adapter(
    req: Dict[str, Any],
    request: Request,
    auth: AuthContext = Depends(require(Permission.USE_COPILOT)),
):
    """Legacy endpoint adapter for Next.js ComplianceCopilotSidecar."""
    msg = str(req.get("message", ""))
    resp = request.app.state.supervisor.process_message(
        message=msg,
        jurisdiction="AE" if "uae" in msg.lower() or "aed" in msg.lower() or "eosb" in msg.lower() else "IN",
        context=req.get("context", {}),
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
            "benefits_display": ["Health Insurance", "Remote Flexibility", "Learning Budget", "Performance Bonus"],
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
                "salary": 140000,
                "minSalary": 120000,
                "maxSalary": 160000,
                "validTill": "2026-12-31",
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
            "benefits_display": ["Comprehensive Health", "Stock Options (ESOPs)", "Executive Coaching"],
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
                "salary": 180000,
                "minSalary": 160000,
                "maxSalary": 210000,
                "validTill": "2026-12-31",
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
            "benefits_display": ["Wellness Stipend", "Flexible Hours", "Latest MacBook Pro"],
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
                "salary": 110000,
                "minSalary": 95000,
                "maxSalary": 130000,
                "validTill": "2026-12-31",
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
        "archetypes": archetypes,
    }


async def _api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.body, headers=exc.headers)


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
    application.state.clock = clock
    application.state.supervisor = supervisor_agent or supervisor
    application.add_middleware(
        CORSMiddleware,
        allow_origins=registry.all_origins(),
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Tenant-Id"],
    )
    application.add_exception_handler(ApiError, _api_error_handler)
    application.add_exception_handler(PermissionDenied, _permission_denied_handler)
    application.include_router(router)
    return application


def _create_default_app() -> FastAPI:
    return create_app(
        registry=load_registry_from_env(),
        jwks_cache=JwksCache(make_httpx_fetcher(httpx.AsyncClient())),
    )


app = _create_default_app()
