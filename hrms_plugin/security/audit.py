"""Enterprise Audit Gateway and Action Attribution Tracing."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from pydantic import BaseModel


class HostAuditHeaders(BaseModel):
    plugin_version: str = "2.0.0"
    agent_invocation_id: str
    agent_name: str
    hitl_approver_id: str
    tenant_id: str
    reasoning_hash: str
    timestamp_utc: str

    def to_http_headers(self) -> Dict[str, str]:
        """Convert into standardized enterprise HTTP tracing headers."""
        return {
            "X-Plugin-Version": self.plugin_version,
            "X-Agent-Invocation-Id": self.agent_invocation_id,
            "X-Agent-Name": self.agent_name,
            "X-HITL-Approver-Id": self.hitl_approver_id,
            "X-Tenant-Id": self.tenant_id,
            "X-Reasoning-Hash": self.reasoning_hash,
            "X-Trace-Timestamp": self.timestamp_utc,
        }


class HostAuditGateway:
    """Generates traceable audit attribution envelopes for host HRMS write operations."""

    def create_attribution_headers(
        self,
        tenant_id: str,
        agent_name: str,
        hitl_approver_id: str,
        reasoning_payload: Dict[str, Any],
        invocation_id: Optional[str] = None,
    ) -> HostAuditHeaders:
        """Create signed audit attribution headers with SHA-256 reasoning hash."""
        inv_id = invocation_id or f"inv_{uuid.uuid4().hex[:12]}"
        now_utc = datetime.now(timezone.utc).isoformat()

        # Compute deterministic reasoning payload hash
        serialized_reasoning = json.dumps(reasoning_payload, sort_keys=True, default=str)
        reasoning_hash = f"sha256:{hashlib.sha256(serialized_reasoning.encode('utf-8')).hexdigest()}"

        return HostAuditHeaders(
            plugin_version="2.0.0",
            agent_invocation_id=inv_id,
            agent_name=agent_name,
            hitl_approver_id=hitl_approver_id,
            tenant_id=tenant_id,
            reasoning_hash=reasoning_hash,
            timestamp_utc=now_utc,
        )
