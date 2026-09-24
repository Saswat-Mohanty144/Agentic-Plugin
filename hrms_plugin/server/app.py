"""FastAPI REST Service Surface for the Standalone Agentic HRMS AI Plugin."""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

from hrms_plugin.agents.compliance import ComplianceAgent
from hrms_plugin.agents.leave_attendance import LeaveAttendanceAgent
from hrms_plugin.agents.recruitment import RecruitmentAgent
from hrms_plugin.agents.statutory_payroll import StatutoryPayrollAgent
from hrms_plugin.agents.supervisor import AgentResponse, SupervisorAgent
from hrms_plugin.rag.store import Jurisdiction, StatutoryKnowledgeBase
from hrms_plugin.schema.canonical import EntityType, CanonicalDomain
from hrms_plugin.schema.introspector import SchemaIntrospector
from hrms_plugin.schema.mapping import EntityMapping, FieldMap
from hrms_plugin.schema.synthesizer import MappingSynthesizer
from hrms_plugin.security.audit import HostAuditGateway
from hrms_plugin.security.masking import PiiMaskingGateway
from hrms_plugin.security.rbac import AuthContext, Permission, RbacPolicyEnforcer, UserRole

app = FastAPI(
    title="Agentic HRMS AI Plugin Service",
    version="2.0.0",
    description="Universal, host-agnostic AI copilot and statutory compliance sidecar for HRMS suites.",
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
pii_gateway = PiiMaskingGateway()
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
def chat_endpoint(
    req: ChatRequest,
    x_tenant_id: str = Header(default="DEFAULT", alias="X-Tenant-Id"),
):
    """Unified conversational AI copilot endpoint with automatic PII masking and intent routing."""
    text_to_process = req.message
    if req.mask_pii:
        text_to_process, _ = pii_gateway.mask_text(req.message, tenant_id=x_tenant_id)

    response = supervisor.process_message(
        message=text_to_process,
        jurisdiction=req.jurisdiction,
        context=req.context,
    )

    # De-tokenize if masked
    if req.mask_pii:
        response.reply_text = pii_gateway.unmask_text(response.reply_text, tenant_id=x_tenant_id)

    return response


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
def synthesize_mapping(req: SynthesizeRequest):
    """Synthesize candidate FieldMap connecting discovered host fields to Canonical ontology."""
    domain_enum = EntityType(req.domain.upper())
    # Create synthetic DiscoveredEntity from fields
    from hrms_plugin.schema.introspector import DiscoveredEntity, DiscoveredField
    discovered_fields_dict = {
        f["name"]: DiscoveredField(name=f["name"], path=f.get("path", f["name"]))
        for f in req.discovered_fields
    }
    discovered = DiscoveredEntity(name=domain_enum.value, fields=discovered_fields_dict)
    mapping, report = synthesizer.synthesize(
        discovered=discovered,
        canonical_type=domain_enum,
    )
    return report.model_dump()


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
