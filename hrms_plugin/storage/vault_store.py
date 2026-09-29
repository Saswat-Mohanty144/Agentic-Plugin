"""Persistent Vault Store for PII Tokens and Detokenization."""

from __future__ import annotations

import hashlib
from typing import Dict, Optional

from hrms_plugin.storage.base import BaseKeyValueStore
from hrms_plugin.storage.sqlite_store import SqliteKeyValueStore


class PiiVaultStore:
    """Manages persistent tokenization entries across restarts and multi-node clusters."""

    NAMESPACE = "pii_vault"
    LOOKUP_NAMESPACE = "pii_vault_lookup"

    def __init__(self, backend: Optional[BaseKeyValueStore] = None) -> None:
        self.backend: BaseKeyValueStore = backend or SqliteKeyValueStore()

    @staticmethod
    def _make_lookup_key(tenant_id: str, raw_val: str) -> str:
        digest = hashlib.sha256(raw_val.encode("utf-8")).hexdigest()
        return f"{tenant_id}:{digest}"

    def get_by_token(self, token_str: str) -> Optional[Dict[str, str]]:
        """Retrieve token data (token, original_value, entity_type, tenant_id) by token string."""
        return self.backend.get(self.NAMESPACE, token_str)

    def find_existing_token(self, raw_val: str, tenant_id: str) -> Optional[str]:
        """Check if raw value was already tokenized for this tenant."""
        lookup_key = self._make_lookup_key(tenant_id, raw_val)
        res = self.backend.get(self.LOOKUP_NAMESPACE, lookup_key)
        if res and "token" in res:
            return res["token"]
        return None

    def store_token(
        self,
        token_str: str,
        original_value: str,
        entity_type: str,
        tenant_id: str,
        ttl_seconds: Optional[float] = None,
    ) -> None:
        """Persist a token entry and lookup index."""
        data = {
            "token": token_str,
            "original_value": original_value,
            "entity_type": entity_type,
            "tenant_id": tenant_id,
        }
        self.backend.set(self.NAMESPACE, token_str, data, ttl_seconds=ttl_seconds)
        lookup_key = self._make_lookup_key(tenant_id, raw_val=original_value)
        self.backend.set(self.LOOKUP_NAMESPACE, lookup_key, {"token": token_str}, ttl_seconds=ttl_seconds)

    def get_all_for_tenant(self, tenant_id: str) -> Dict[str, Dict[str, str]]:
        """Retrieve all tokens belonging to a tenant."""
        all_entries = self.backend.get_all(self.NAMESPACE)
        return {
            tok: data
            for tok, data in all_entries.items()
            if data.get("tenant_id") == tenant_id
        }

    def clear(self) -> None:
        self.backend.clear(self.NAMESPACE)
        self.backend.clear(self.LOOKUP_NAMESPACE)
