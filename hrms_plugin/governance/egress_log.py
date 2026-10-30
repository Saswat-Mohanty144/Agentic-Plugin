"""Content-free audit log of every LLM call (piece G, decision G1)."""

from __future__ import annotations

import secrets
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from hrms_plugin.governance import disclosure
from hrms_plugin.storage.base import BaseKeyValueStore

NAMESPACE = "llm_egress"
SUBJECT_NAMESPACE = "llm_egress_subjects"
DAILY_NAMESPACE = "llm_egress_daily"  # running token total per tenant and UTC day (agy H-F4: no full scans)
MAX_LISTED = 200
DAY_SECONDS = 86400
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


class EgressLog:
    def __init__(self, store: BaseKeyValueStore, clock: Callable[[], float] = time.time):
        self._store = store
        self._clock = clock
        self._lock = threading.Lock()

    @staticmethod
    def _day_key(tenant_id: str, at: float) -> str:
        return f"{tenant_id}:{int(at // DAY_SECONDS)}"

    @staticmethod
    def _tokens(row: Dict[str, Any]) -> int:
        return sum(row.get(f) or 0 for f in TOKEN_FIELDS)

    def _index_subject_refs(self, tenant_id: str, row_key: str, subject_refs: List[Dict[str, Any]]) -> None:
        for item in subject_refs:
            ref = item.get("ref")
            if not ref:
                continue
            idx_key = f"{tenant_id}:{ref}"
            entry = self._store.get(SUBJECT_NAMESPACE, idx_key) or {}
            keys = list(entry.get("keys", []))
            if row_key not in keys:
                keys.append(row_key)
                self._store.set(SUBJECT_NAMESPACE, idx_key, {"keys": keys})

    def record(self, *, tenant_id: str, provider: str, model: str, payload_sha256: str,
               redacted_kinds: Dict[str, int], input_tokens: Optional[int] = None,
               output_tokens: Optional[int] = None, status: str = "SENT",
               channel: str = "llm", subject_refs: Optional[List[Dict[str, Any]]] = None) -> str:
        """Record an outgoing call. Written BEFORE the provider is called, so failures are logged too (agy G-F9)."""
        subject_index = disclosure.is_configured()
        actual_refs = list(subject_refs or []) if subject_index else []
        row = {"tenant_id": tenant_id, "at": self._clock(), "provider": provider, "model": model,
               "payload_sha256": payload_sha256, "redacted_kinds": dict(redacted_kinds),
               "input_tokens": input_tokens, "output_tokens": output_tokens, "status": status,
               "channel": channel, "subject_refs": actual_refs, "subject_index": subject_index}
        key = f"{tenant_id}:{row['at']:017.6f}:{secrets.token_hex(4)}"
        self._store.set(NAMESPACE, key, row)
        if subject_index and actual_refs:
            self._index_subject_refs(tenant_id, key, actual_refs)
        return key

    def update(self, key: str, **fields: Any) -> None:
        with self._lock:
            row = self._store.get(NAMESPACE, key)
            if row is None:
                return
            before = self._tokens(row)
            if "subject_refs" in fields:
                if disclosure.is_configured():
                    refs = list(fields["subject_refs"] or [])
                    fields["subject_refs"] = refs
                    fields["subject_index"] = True
                    self._index_subject_refs(row["tenant_id"], key, refs)
                else:
                    fields["subject_refs"] = []
                    fields["subject_index"] = False
            row.update(fields)
            self._store.set(NAMESPACE, key, row)
            delta = self._tokens(row) - before
            if delta:
                day_key = self._day_key(row["tenant_id"], row["at"])
                total = (self._store.get(DAILY_NAMESPACE, day_key) or {}).get("tokens", 0)
                self._store.set(DAILY_NAMESPACE, day_key, {"tokens": total + delta})

    def list(self, tenant_id: str) -> List[Dict[str, Any]]:
        rows = [r for k, r in self._store.get_all(NAMESPACE).items() if k.startswith(f"{tenant_id}:")]
        return sorted(rows, key=lambda r: r["at"], reverse=True)[:MAX_LISTED]

    def purge(self, tenant_id: str, retention_days: int) -> int:
        """Delete this tenant's rows older than the retention period (QF); returns how many were removed."""
        cutoff = self._clock() - retention_days * DAY_SECONDS
        old = [(k, r) for k, r in self._store.get_all(NAMESPACE).items()
               if k.startswith(f"{tenant_id}:") and r.get("at", 0) < cutoff]
        for key, row in old:
            for item in row.get("subject_refs", []):
                ref = item.get("ref")
                if not ref:
                    continue
                idx_key = f"{tenant_id}:{ref}"
                entry = self._store.get(SUBJECT_NAMESPACE, idx_key)
                if entry and "keys" in entry:
                    keys = [k for k in entry["keys"] if k != key]
                    if keys:
                        self._store.set(SUBJECT_NAMESPACE, idx_key, {"keys": keys})
                    else:
                        self._store.delete(SUBJECT_NAMESPACE, idx_key)
            self._store.delete(NAMESPACE, key)
        return len(old)

    def delete_tenant(self, tenant_id: str) -> int:
        """Deletion on request (QF): every egress row of this tenant. Daily token counters are kept for budgets."""
        keys = [k for k in self._store.get_all(NAMESPACE) if k.startswith(f"{tenant_id}:")]
        for key in keys:
            self._store.delete(NAMESPACE, key)
        subj_keys = [k for k in self._store.get_all(SUBJECT_NAMESPACE) if k.startswith(f"{tenant_id}:")]
        for k in subj_keys:
            self._store.delete(SUBJECT_NAMESPACE, k)
        return len(keys)

    def tokens_today(self, tenant_id: str) -> int:
        """Every token this tenant used in the current UTC day (one counter read, not a scan)."""
        row = self._store.get(DAILY_NAMESPACE, self._day_key(tenant_id, self._clock()))
        return int((row or {}).get("tokens", 0))
