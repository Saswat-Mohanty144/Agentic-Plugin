"""Content-free audit log of every LLM call (piece G, decision G1)."""

from __future__ import annotations

import secrets
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from hrms_plugin.storage.base import BaseKeyValueStore

NAMESPACE = "llm_egress"
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

    def record(self, *, tenant_id: str, provider: str, model: str, payload_sha256: str,
               redacted_kinds: Dict[str, int], input_tokens: Optional[int] = None,
               output_tokens: Optional[int] = None, status: str = "SENT") -> str:
        """Record an outgoing call. Written BEFORE the provider is called, so failures are logged too (agy G-F9)."""
        row = {"tenant_id": tenant_id, "at": self._clock(), "provider": provider, "model": model,
               "payload_sha256": payload_sha256, "redacted_kinds": dict(redacted_kinds),
               "input_tokens": input_tokens, "output_tokens": output_tokens, "status": status}
        key = f"{tenant_id}:{row['at']:017.6f}:{secrets.token_hex(4)}"
        self._store.set(NAMESPACE, key, row)
        return key

    def update(self, key: str, **fields: Any) -> None:
        with self._lock:
            row = self._store.get(NAMESPACE, key)
            if row is None:
                return
            before = self._tokens(row)
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
        old = [k for k, r in self._store.get_all(NAMESPACE).items()
               if k.startswith(f"{tenant_id}:") and r.get("at", 0) < cutoff]
        for key in old:
            self._store.delete(NAMESPACE, key)
        return len(old)

    def delete_tenant(self, tenant_id: str) -> int:
        """Deletion on request (QF): every egress row of this tenant. Daily token counters are kept for budgets."""
        keys = [k for k in self._store.get_all(NAMESPACE) if k.startswith(f"{tenant_id}:")]
        for key in keys:
            self._store.delete(NAMESPACE, key)
        return len(keys)

    def tokens_today(self, tenant_id: str) -> int:
        """Every token this tenant used in the current UTC day (one counter read, not a scan)."""
        row = self._store.get(DAILY_NAMESPACE, self._day_key(tenant_id, self._clock()))
        return int((row or {}).get("tokens", 0))
