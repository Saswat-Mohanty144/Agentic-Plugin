"""Base interfaces and protocols for persistent key-value and schema storage."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class BaseKeyValueStore(ABC):
    """Abstract interface for pluggable key-value storage backends (SQLite, Redis, Memory)."""

    @abstractmethod
    def get(self, namespace: str, key: str) -> Optional[Dict[str, Any]]:
        """Retrieve a stored JSON-compatible dictionary by namespace and key."""
        pass

    @abstractmethod
    def set(self, namespace: str, key: str, value: Dict[str, Any], ttl_seconds: Optional[float] = None) -> None:
        """Store a JSON-compatible dictionary under namespace and key with optional TTL."""
        pass

    @abstractmethod
    def delete(self, namespace: str, key: str) -> bool:
        """Delete a key from a namespace. Returns True if key was deleted."""
        pass

    @abstractmethod
    def list_keys(self, namespace: str) -> List[str]:
        """List all unexpired keys within a given namespace."""
        pass

    @abstractmethod
    def get_all(self, namespace: str) -> Dict[str, Dict[str, Any]]:
        """Retrieve all key-value entries in a given namespace."""
        pass

    @abstractmethod
    def clear(self, namespace: Optional[str] = None) -> None:
        """Clear all entries in a namespace, or all namespaces if None."""
        pass

    @abstractmethod
    def close(self) -> None:
        """Close underlying connection resources."""
        pass
