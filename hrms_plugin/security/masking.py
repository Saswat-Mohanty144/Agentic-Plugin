"""Zero-Trust PII Masking and Vault Tokenization Gateway."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class PiiVaultEntry:
    token: str
    original_value: str
    entity_type: str  # "AADHAAR", "PAN", "SSN", "EMIRATES_ID", "BANK_ACCOUNT", "PHONE", "SALARY"
    tenant_id: str


class PiiMaskingGateway:
    """Pre-prompt PII redaction and reversible vault tokenization engine."""

    # Regex patterns for identifying sensitive credentials & identifiers
    PATTERNS = {
        "AADHAAR": re.compile(r"\b\d{4}[ -]?\d{4}[ -]?\d{4}\b"),
        "PAN": re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]{1}\b"),
        "EMIRATES_ID": re.compile(r"\b784[ -]?\d{4}[ -]?\d{7}[ -]?\d{1}\b"),
        "SSN": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
        "EMAIL": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"),
        "PHONE": re.compile(r"(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"),
    }

    def __init__(self):
        # In-memory ephemeral vault keyed by token string
        self._vault: Dict[str, PiiVaultEntry] = {}

    def mask_text(
        self,
        text: str,
        tenant_id: str = "DEFAULT",
        mask_emails: bool = False,
    ) -> Tuple[str, List[PiiVaultEntry]]:
        """Redact sensitive PII and replace with deterministic vault tokens.

        Returns masked string and list of generated vault tokens.
        """
        masked = text
        generated_entries: List[PiiVaultEntry] = []

        for entity_type, pattern in self.PATTERNS.items():
            if entity_type == "EMAIL" and not mask_emails:
                continue

            matches = list(pattern.finditer(masked))
            # Replace in reverse order to preserve string indices
            for match in reversed(matches):
                raw_val = match.group(0)
                # Check if this raw value was already tokenized for this tenant
                existing_token = None
                for t, entry in self._vault.items():
                    if entry.original_value == raw_val and entry.tenant_id == tenant_id:
                        existing_token = t
                        break

                if not existing_token:
                    token_id = uuid.uuid4().hex[:8].upper()
                    token_str = f"[VAULT_{entity_type}_{token_id}]"
                    entry = PiiVaultEntry(
                        token=token_str,
                        original_value=raw_val,
                        entity_type=entity_type,
                        tenant_id=tenant_id,
                    )
                    self._vault[token_str] = entry
                    generated_entries.append(entry)
                else:
                    token_str = existing_token

                start, end = match.span()
                masked = masked[:start] + token_str + masked[end:]

        return masked, generated_entries

    def mask_record_dict(
        self,
        record: Dict[str, Any],
        tenant_id: str = "DEFAULT",
        sensitive_keys: Optional[Set[str]] = None,
    ) -> Dict[str, Any]:
        """Deep mask dictionaries containing sensitive fields like compensation or national IDs."""
        keys_to_mask = sensitive_keys or {
            "aadhaar", "pan", "ssn", "emirates_id", "national_id",
            "bank_account", "account_number", "iban", "salary", "basic_salary", "ctc"
        }

        masked_record: Dict[str, Any] = {}
        for k, v in record.items():
            if isinstance(v, str):
                if k.lower() in keys_to_mask:
                    token_id = uuid.uuid4().hex[:8].upper()
                    token_str = f"[VAULT_{k.upper()}_{token_id}]"
                    entry = PiiVaultEntry(
                        token=token_str,
                        original_value=v,
                        entity_type=k.upper(),
                        tenant_id=tenant_id,
                    )
                    self._vault[token_str] = entry
                    masked_record[k] = token_str
                else:
                    text_masked, _ = self.mask_text(v, tenant_id=tenant_id)
                    masked_record[k] = text_masked
            elif isinstance(v, (int, float)) and k.lower() in keys_to_mask:
                token_id = uuid.uuid4().hex[:8].upper()
                token_str = f"[VAULT_{k.upper()}_{token_id}]"
                entry = PiiVaultEntry(
                    token=token_str,
                    original_value=str(v),
                    entity_type=k.upper(),
                    tenant_id=tenant_id,
                )
                self._vault[token_str] = entry
                masked_record[k] = token_str
            elif isinstance(v, dict):
                masked_record[k] = self.mask_record_dict(v, tenant_id=tenant_id, sensitive_keys=keys_to_mask)
            elif isinstance(v, list):
                masked_record[k] = [
                    self.mask_record_dict(item, tenant_id=tenant_id, sensitive_keys=keys_to_mask)
                    if isinstance(item, dict) else item
                    for item in v
                ]
            else:
                masked_record[k] = v

        return masked_record

    def unmask_text(self, text: str, tenant_id: str = "DEFAULT") -> str:
        """Re-hydrate vault tokens back to original values on egress."""
        unmasked = text
        for token_str, entry in self._vault.items():
            if entry.tenant_id == tenant_id and token_str in unmasked:
                unmasked = unmasked.replace(token_str, entry.original_value)
        return unmasked
