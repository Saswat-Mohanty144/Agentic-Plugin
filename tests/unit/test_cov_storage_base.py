"""Coverage tests for hrms_plugin/storage/base.py."""

from __future__ import annotations

import pytest

from hrms_plugin.storage.base import BaseKeyValueStore


class ConcreteStore(BaseKeyValueStore):
    """Subclass invoking super() to verify abstract base pass implementations."""

    def get(self, namespace: str, key: str):
        return super().get(namespace, key)

    def set(self, namespace: str, key: str, value, ttl_seconds=None):
        return super().set(namespace, key, value, ttl_seconds)

    def delete(self, namespace: str, key: str):
        return super().delete(namespace, key)

    def list_keys(self, namespace: str):
        return super().list_keys(namespace)

    def get_all(self, namespace: str):
        return super().get_all(namespace)

    def clear(self, namespace=None):
        return super().clear(namespace)

    def close(self):
        return super().close()


def test_cannot_instantiate_abstract_base_store():
    """Negative test: BaseKeyValueStore cannot be directly instantiated."""
    with pytest.raises(TypeError) as excinfo:
        BaseKeyValueStore()  # type: ignore[abstract]
    assert "Can't instantiate abstract class BaseKeyValueStore" in str(excinfo.value)


def test_base_store_super_methods_return_none():
    """Positive test: Concrete implementation calling super() methods returns expected None."""
    store = ConcreteStore()
    assert store.get("test_ns", "key1") is None
    assert store.set("test_ns", "key1", {"val": 1}, ttl_seconds=30.0) is None
    assert store.delete("test_ns", "key1") is None
    assert store.list_keys("test_ns") is None
    assert store.get_all("test_ns") is None
    assert store.clear("test_ns") is None
    assert store.clear(None) is None
    assert store.close() is None
