"""Enterprise Audit Gateway and Action Attribution Tracing."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import BaseModel

AUDIT_HEADER_NAMES = {
    "X-Plugin-Version",
    "X-Agent-Invocation-Id",
    "X-Agent-Name",
    "X-HITL-Approver-Id",
    "X-Tenant-Id",
    "X-Reasoning-Hash",
    "X-Trace-Timestamp",
    "X-Audit-Key-Id",
}


class AuditSigningError(Exception):
    def __init__(self, code: str = "AUDIT_SIGNING_NOT_CONFIGURED", detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class HostAuditHeaders(BaseModel):
    plugin_version: str = "2.0.0"
    agent_invocation_id: str
    agent_name: str
    hitl_approver_id: str
    tenant_id: str
    reasoning_hash: str
    timestamp_utc: str
    audit_key_id: Optional[str] = None
    audit_signature: Optional[str] = None

    def to_http_headers(self) -> Dict[str, str]:
        """Convert into standardized enterprise HTTP tracing headers with Ed25519 signature."""
        headers = {
            "X-Plugin-Version": self.plugin_version,
            "X-Agent-Invocation-Id": self.agent_invocation_id,
            "X-Agent-Name": self.agent_name,
            "X-HITL-Approver-Id": self.hitl_approver_id,
            "X-Tenant-Id": self.tenant_id,
            "X-Reasoning-Hash": self.reasoning_hash,
            "X-Trace-Timestamp": self.timestamp_utc,
        }
        if self.audit_key_id:
            headers["X-Audit-Key-Id"] = self.audit_key_id
        if self.audit_signature:
            headers["X-Audit-Signature"] = self.audit_signature
        return headers


def verify_audit_headers(headers: Dict[str, str], public_key: Any) -> bool:
    """Verify Ed25519 signature over audit headers."""
    try:
        if isinstance(public_key, Ed25519PublicKey):
            pub = public_key
        elif isinstance(public_key, bytes):
            if len(public_key) == 32:
                pub = Ed25519PublicKey.from_public_bytes(public_key)
            else:
                pub = serialization.load_pem_public_key(public_key)
        elif isinstance(public_key, str):
            pub = serialization.load_pem_public_key(public_key.encode("utf-8"))
        else:
            return False

        sig_b64 = None
        for k, v in headers.items():
            if k.lower() == "x-audit-signature":
                sig_b64 = v
                break
        if not sig_b64:
            return False

        padding = "=" * ((4 - len(sig_b64) % 4) % 4)
        sig_bytes = base64.urlsafe_b64decode(sig_b64 + padding)

        known_lower = {name.lower(): name for name in AUDIT_HEADER_NAMES}
        audit_items = {k: v for k, v in headers.items() if k.lower() != "x-audit-signature"}
        if any(k.lower() not in known_lower for k in audit_items):
            if any(k.lower() in known_lower for k in audit_items):
                audit_items = {
                    known_lower[k.lower()]: str(v) for k, v in audit_items.items() if k.lower() in known_lower
                }

        lines = [f"{k}:{v}" for k, v in sorted(audit_items.items())]
        msg = "\n".join(lines).encode("utf-8")
        pub.verify(sig_bytes, msg)
        return True
    except Exception:
        return False


class HostAuditGateway:
    """Generates traceable audit attribution envelopes for host HRMS write operations."""

    def __init__(self, key_file: Optional[str] = None, key_id: Optional[str] = None):
        self._key_file = key_file
        self._key_id = key_id

    def load_signing_key(self) -> Tuple[Ed25519PrivateKey, str]:
        key_file = self._key_file or os.environ.get("HRMS_AUDIT_SIGNING_KEY_FILE")
        key_id = self._key_id or os.environ.get("HRMS_AUDIT_KEY_ID")
        if not key_file or not key_id:
            raise AuditSigningError("AUDIT_SIGNING_NOT_CONFIGURED", "Key file or key ID not set in environment")
        path = Path(key_file)
        if not path.is_file():
            raise AuditSigningError("AUDIT_SIGNING_NOT_CONFIGURED", f"Key file does not exist: {key_file}")
        try:
            pem_bytes = path.read_bytes()
            key = serialization.load_pem_private_key(pem_bytes, password=None)
            if not isinstance(key, Ed25519PrivateKey):
                raise AuditSigningError("AUDIT_SIGNING_NOT_CONFIGURED", "Key is not an Ed25519 private key")
            return key, key_id
        except AuditSigningError:
            raise
        except Exception as exc:
            raise AuditSigningError("AUDIT_SIGNING_NOT_CONFIGURED", f"Unreadable key: {exc}") from exc

    def is_configured(self) -> bool:
        try:
            self.load_signing_key()
            return True
        except AuditSigningError:
            return False

    def create_attribution_headers(
        self,
        tenant_id: str,
        agent_name: str,
        hitl_approver_id: str,
        reasoning_payload: Dict[str, Any],
        invocation_id: Optional[str] = None,
    ) -> HostAuditHeaders:
        """Create signed audit attribution headers with SHA-256 reasoning hash and Ed25519 signature."""
        priv_key, key_id = self.load_signing_key()
        inv_id = invocation_id or f"inv_{uuid.uuid4().hex[:12]}"
        now_utc = datetime.now(timezone.utc).isoformat()

        serialized_reasoning = json.dumps(reasoning_payload, sort_keys=True, default=str)
        reasoning_hash = f"sha256:{hashlib.sha256(serialized_reasoning.encode('utf-8')).hexdigest()}"

        base_headers = {
            "X-Plugin-Version": "2.0.0",
            "X-Agent-Invocation-Id": inv_id,
            "X-Agent-Name": agent_name,
            "X-HITL-Approver-Id": hitl_approver_id,
            "X-Tenant-Id": tenant_id,
            "X-Reasoning-Hash": reasoning_hash,
            "X-Trace-Timestamp": now_utc,
            "X-Audit-Key-Id": key_id,
        }
        lines = [f"{k}:{v}" for k, v in sorted(base_headers.items())]
        msg = "\n".join(lines).encode("utf-8")
        sig = priv_key.sign(msg)
        sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")

        return HostAuditHeaders(
            plugin_version="2.0.0",
            agent_invocation_id=inv_id,
            agent_name=agent_name,
            hitl_approver_id=hitl_approver_id,
            tenant_id=tenant_id,
            reasoning_hash=reasoning_hash,
            timestamp_utc=now_utc,
            audit_key_id=key_id,
            audit_signature=sig_b64,
        )
