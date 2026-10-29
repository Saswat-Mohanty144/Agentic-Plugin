"""Content-free audit log of every LLM call (piece G, decision G1)."""

from __future__ import annotations

import secrets
import time
from typing import Any, Callable, Dict, List, Optional

from hrms_plugin.storage.base import BaseKeyValueStore

NAMESPACE = "llm_egress"
MAX_LISTED = 200


class EgressLog:
    def __init__(self, store: BaseKeyValueStore, clock: Callable[[], float] = time.time):
        self._store = store
        self._clock = clock

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
        row = self._store.get(NAMESPACE, key)
        if row is not None:
            row.update(fields)
            self._store.set(NAMESPACE, key, row)

    def list(self, tenant_id: str) -> List[Dict[str, Any]]:
        rows = [r for k, r in self._store.get_all(NAMESPACE).items() if k.startswith(f"{tenant_id}:")]
        return sorted(rows, key=lambda r: r["at"], reverse=True)[:MAX_LISTED]
