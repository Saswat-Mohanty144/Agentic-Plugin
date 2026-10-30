"""Fixtures for audit test suite ensuring isolation and cleanup of global state."""

import pytest

from hrms_plugin.server.app import _storage


@pytest.fixture(autouse=True)
def restore_audit_storage_state():
    """A10: Restore global storage keys modified by audit tests."""
    with _storage._lock, _storage._get_connection() as conn:
        snapshot = conn.execute("SELECT namespace, key, value, expires_at, updated_at FROM kv_store").fetchall()
    try:
        yield
    finally:
        with _storage._lock, _storage._get_connection() as conn:
            conn.execute("DELETE FROM kv_store")
            conn.executemany(
                "INSERT INTO kv_store (namespace, key, value, expires_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                snapshot,
            )
            conn.commit()
