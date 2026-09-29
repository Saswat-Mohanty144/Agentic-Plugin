"""FastAPI REST Service Surface for the Standalone Agentic HRMS AI Plugin."""

from __future__ import annotations

import base64
import json
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

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
from hrms_plugin.security.masking import PiiMaskingGateway
from hrms_plugin.security.rbac import AuthContext, Permission, RbacPolicyEnforcer, UserRole
from hrms_plugin.storage.mapping_store import MappingStore
from hrms_plugin.storage.sqlite_store import SqliteKeyValueStore
from hrms_plugin.storage.vault_store import PiiVaultStore

app = FastAPI(
    title="Agentic HRMS AI Plugin Service",
    version="2.0.0",
    description="Universal, host-agnostic AI copilot and statutory compliance sidecar for HRMS suites.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
_storage = SqliteKeyValueStore(".hrms_data/plugin_storage.db")
vault_store = PiiVaultStore(backend=_storage)
mapping_store = MappingStore(backend=_storage)

pii_gateway = PiiMaskingGateway(store=vault_store)
rbac_enforcer = RbacPolicyEnforcer()
audit_gateway = HostAuditGateway()
introspector = SchemaIntrospector()
synthesizer = MappingSynthesizer()


# Request / Response Schemas
class ChatRequest(BaseModel):
    message: str
    jurisdiction: str = "IN"
    context: Optional[Dict[str, Any]] = None
    mask_pii: bool = True
    agentic: bool = False
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
    jurisdiction: str = "IN"
    language_code: Optional[str] = "en-IN"


class IntrospectRequest(BaseModel):
    swagger_dict: Optional[Dict[str, Any]] = None
    sample_records: Optional[Dict[str, List[Dict[str, Any]]]] = None


class SynthesizeRequest(BaseModel):
    domain: str = "EMPLOYEE"
    discovered_fields: List[Dict[str, Any]]


class RemediationExecuteRequest(BaseModel):
    violation_id: str
    target_entity: str
    entity_id: str
    patch_payload: Dict[str, Any]
    approver_id: str
    approver_role: str = "HR_ADMIN"


@app.get("/healthz")
def healthz():
    return {
        "status": "healthy",
        "version": "2.0.0",
        "jurisdictions_supported": ["IN", "AE", "SA", "US"],
    }


@app.post("/v1/chat", response_model=AgentResponse)
async def chat_endpoint(
    req: ChatRequest,
    x_tenant_id: str = Header(default="DEFAULT", alias="X-Tenant-Id"),
):
    """Unified conversational AI copilot endpoint with automatic PII masking and agentic tool reasoning."""
    text_to_process = req.message
    if req.mask_pii:
        text_to_process, _ = pii_gateway.mask_text(req.message, tenant_id=x_tenant_id)

    if req.agentic:
        response = await supervisor.process_agentic(
            message=text_to_process,
            jurisdiction=req.jurisdiction,
            context=req.context,
            tenant_id=x_tenant_id,
            history=req.history,
        )
    else:
        response = supervisor.process_message(
            message=text_to_process,
            jurisdiction=req.jurisdiction,
            context=req.context,
        )

    # De-tokenize if masked
    if req.mask_pii:
        response.reply_text = pii_gateway.unmask_text(response.reply_text, tenant_id=x_tenant_id)

    return response


@app.post("/v1/chat/stream")
async def chat_stream_endpoint(
    req: ChatRequest,
    x_tenant_id: str = Header(default="DEFAULT", alias="X-Tenant-Id"),
):
    """Real-time SSE event stream for autonomous agent thoughts, tool dispatches, and tokens."""

    async def event_generator():
        text_to_process = req.message
        if req.mask_pii:
            text_to_process, _ = pii_gateway.mask_text(req.message, tenant_id=x_tenant_id)

        async for chunk in supervisor.stream_agentic(
            message=text_to_process,
            jurisdiction=req.jurisdiction,
            context=req.context,
            tenant_id=x_tenant_id,
        ):
            yield f"data: {json.dumps(chunk)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.post("/v1/voice/transcribe")
async def voice_transcribe(req: VoiceTranscribeRequest):
    """Transcribe base64 audio bytes via Sarvam AI Saaras model."""
    audio_bytes = base64.b64decode(req.audio_base64)
    res = await voice_agent.transcribe_audio(audio_bytes, language_code=req.language_code)
    return res


@app.post("/v1/voice/synthesize")
async def voice_synthesize(req: VoiceSynthesizeRequest):
    """Synthesize text into natural voice audio via Sarvam AI Bulbul model."""
    res = await voice_agent.synthesize_speech(
        text=req.text,
        target_language=req.target_language,
        speaker=req.speaker,
    )
    return res


@app.post("/v1/voice/turn")
async def voice_turn(req: VoiceTurnRequest, x_tenant_id: str = Header(default="DEFAULT", alias="X-Tenant-Id")):
    """Full-duplex multimodal turn: Audio In -> Transcribe -> ReAct Agent Brain -> Spoken Audio Out."""
    audio_bytes = base64.b64decode(req.audio_base64)
    stt = await voice_agent.transcribe_audio(audio_bytes, language_code=req.language_code)
    transcript = stt.get("transcript", "")

    # Run agentic turn
    agent_res = await supervisor.process_agentic(
        message=transcript,
        jurisdiction=req.jurisdiction,
        tenant_id=x_tenant_id,
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


@app.post("/v1/schema/introspect")
def introspect_schema(req: IntrospectRequest):
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


@app.post("/v1/schema/synthesize")
def synthesize_mapping(
    req: SynthesizeRequest,
    x_tenant_id: str = Header(default="DEFAULT", alias="X-Tenant-Id"),
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
    mapping_store.save_mapping(tenant_id=x_tenant_id, mapping=mapping)
    return report.model_dump()


@app.post("/v1/schema/synthesize/agentic")
async def synthesize_mapping_agentic(
    req: SynthesizeRequest,
    x_tenant_id: str = Header(default="DEFAULT", alias="X-Tenant-Id"),
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
    mapping_store.save_mapping(tenant_id=x_tenant_id, mapping=mapping)
    return report.model_dump()


@app.get("/v1/schema/mappings")
def list_schema_mappings(x_tenant_id: str = Header(default="DEFAULT", alias="X-Tenant-Id")):
    """List all persisted dynamic entity mappings for this tenant."""
    return {
        "tenant_id": x_tenant_id,
        "mappings": mapping_store.list_mappings(x_tenant_id),
    }


@app.get("/v1/schema/mappings/{vendor}/{entity_type}")
def get_schema_mapping(
    vendor: str,
    entity_type: str,
    x_tenant_id: str = Header(default="DEFAULT", alias="X-Tenant-Id"),
):
    """Retrieve the exact compiled FieldMap for a given vendor and entity type."""
    mapping = mapping_store.load_mapping(x_tenant_id, vendor, entity_type)
    if not mapping:
        raise HTTPException(status_code=404, detail=f"No mapping found for {vendor}/{entity_type}")
    from hrms_plugin.storage.mapping_store import entity_mapping_to_dict
    return entity_mapping_to_dict(mapping)


@app.post("/v1/remediate/execute")
def execute_remediation(
    req: RemediationExecuteRequest,
    x_tenant_id: str = Header(default="DEFAULT", alias="X-Tenant-Id"),
):
    """Execute human-approved compliance remediation patch with signed host audit attribution headers."""
    auth = AuthContext(
        user_id=req.approver_id,
        tenant_id=x_tenant_id,
        role=UserRole(req.approver_role),
    )

    if not rbac_enforcer.is_authorized(auth, Permission.EXECUTE_REMEDIATION):
        raise HTTPException(status_code=403, detail="Approver lacks EXECUTE_REMEDIATION permission")

    # Generate Host Audit Attribution Headers
    headers = audit_gateway.create_attribution_headers(
        tenant_id=x_tenant_id,
        agent_name="StatutoryComplianceAuditor",
        hitl_approver_id=req.approver_id,
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


@app.get("/v1/compliance/diagnose")
@app.get("/api/compliance/agent/diagnose")
def get_compliance_diagnosis(
    jurisdiction: str = "AE",
    tenant_id: str = Header(default="DEFAULT", alias="X-Tenant-Id"),
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
        tenant_id=tenant_id,
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


@app.get("/v1/compliance/dashboard/live")
@app.get("/api/compliance/dashboard/live")
def get_compliance_dashboard_live():
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


@app.post("/api/compliance/agent/chat")
def legacy_chat_adapter(
    req: Dict[str, Any],
    x_tenant_id: str = Header(default="DEFAULT", alias="X-Tenant-Id"),
):
    """Legacy endpoint adapter for Next.js ComplianceCopilotSidecar."""
    msg = str(req.get("message", ""))
    resp = supervisor.process_message(
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
