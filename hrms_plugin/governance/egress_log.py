"""Content-free audit log of every LLM call (piece G, decision G1)."""

from __future__ import annotations

import secrets
import time
from typing import Any, Callable, Dict, List

from hrms_plugin.storage.base import BaseKeyValueStore

NAMESPACE = "llm_egress"
MAX_LISTED = 200


class EgressLog:
    def __init__(self, store: BaseKeyValueStore, clock: Callable[[], float] = time.time):
        self._store = store
        self._clock = clock

    def record(self, *, tenant_id: str, provider: str, model: str, payload_sha256: str,
               redacted_kinds: Dict[str, int], input_tokens: int, output_tokens: int) -> Dict[str, Any]:
        row = {"tenant_id": tenant_id, "at": self._clock(), "provider": provider, "model": model,
               "payload_sha256": payload_sha256, "redacted_kinds": dict(redacted_kinds),
               "input_tokens": int(input_tokens or 0), "output_tokens": int(output_tokens or 0)}
        self._store.set(NAMESPACE, f"{tenant_id}:{row['at']:017.6f}:{secrets.token_hex(4)}", row)
        return row

    def list(self, tenant_id: str) -> List[Dict[str, Any]]:
        rows = [r for k, r in self._store.get_all(NAMESPACE).items() if k.startswith(f"{tenant_id}:")]
        return sorted(rows, key=lambda r: r["at"], reverse=True)[:MAX_LISTED]
