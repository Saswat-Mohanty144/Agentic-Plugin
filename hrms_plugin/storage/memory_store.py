"""In-memory key-value store with TTL support for testing and ephemeral runtimes."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from hrms_plugin.storage.base import BaseKeyValueStore


class MemoryKeyValueStore(BaseKeyValueStore):
    """Thread-safe in-memory key-value store with TTL expiration."""

    def __init__(self) -> None:
        # structure: {namespace: {key: (data_dict, expires_at_timestamp_or_None)}}
        self._data: Dict[str, Dict[str, tuple[Dict[str, Any], Optional[float]]]] = {}

    def _purge_expired(self, namespace: str) -> None:
        if namespace not in self._data:
            return
        now = time.time()
        expired = [
            k
            for k, (_, exp) in self._data[namespace].items()
            if exp is not None and exp < now
        ]
        for k in expired:
            del self._data[namespace][k]

    def get(self, namespace: str, key: str) -> Optional[Dict[str, Any]]:
        self._purge_expired(namespace)
        ns = self._data.get(namespace, {})
        entry = ns.get(key)
        if entry is None:
            return None
        val, exp = entry
        if exp is not None and exp < time.time():
            del ns[key]
            return None
        return dict(val)

    def set(self, namespace: str, key: str, value: Dict[str, Any], ttl_seconds: Optional[float] = None) -> None:
        if namespace not in self._data:
            self._data[namespace] = {}
        exp = time.time() + ttl_seconds if ttl_seconds is not None else None
        self._data[namespace][key] = (dict(value), exp)

    def delete(self, namespace: str, key: str) -> bool:
        if namespace in self._data and key in self._data[namespace]:
            del self._data[namespace][key]
            return True
        return False

    def list_keys(self, namespace: str) -> List[str]:
        self._purge_expired(namespace)
        return list(self._data.get(namespace, {}).keys())

    def get_all(self, namespace: str) -> Dict[str, Dict[str, Any]]:
        self._purge_expired(namespace)
        ns = self._data.get(namespace, {})
        return {k: dict(v[0]) for k, v in ns.items()}

    def clear(self, namespace: Optional[str] = None) -> None:
        if namespace is not None:
            self._data.pop(namespace, None)
        else:
            self._data.clear()

    def close(self) -> None:
        self.clear()
