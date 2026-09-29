"""SQLite-backed persistent Key-Value Store with WAL mode and TTL support.

Zero-dependency persistent storage for single-node, Docker containers, and edge sidecars.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from hrms_plugin.storage.base import BaseKeyValueStore


class SqliteKeyValueStore(BaseKeyValueStore):
    """Persistent, thread-safe Key-Value store backed by SQLite with WAL mode."""

    def __init__(self, db_path: Union[str, Path] = ".hrms_data/plugin_storage.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    @contextlib.contextmanager
    def _get_connection(self):
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._lock, self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kv_store (
                    namespace TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    expires_at REAL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (namespace, key)
                );
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_kv_namespace_expires
                ON kv_store(namespace, expires_at);
                """
            )
            conn.commit()

    def _purge_expired(self, conn: sqlite3.Connection, namespace: Optional[str] = None) -> None:
        now = time.time()
        if namespace:
            conn.execute(
                "DELETE FROM kv_store WHERE namespace = ? AND expires_at IS NOT NULL AND expires_at < ?",
                (namespace, now),
            )
        else:
            conn.execute(
                "DELETE FROM kv_store WHERE expires_at IS NOT NULL AND expires_at < ?",
                (now,),
            )

    def get(self, namespace: str, key: str) -> Optional[Dict[str, Any]]:
        now = time.time()
        with self._lock, self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT value, expires_at FROM kv_store
                WHERE namespace = ? AND key = ?
                """,
                (namespace, key),
            )
            row = cursor.fetchone()
            if not row:
                return None
            val_str, expires_at = row
            if expires_at is not None and expires_at < now:
                conn.execute(
                    "DELETE FROM kv_store WHERE namespace = ? AND key = ?",
                    (namespace, key),
                )
                conn.commit()
                return None
            try:
                return json.loads(val_str)
            except Exception:
                return None

    def set(self, namespace: str, key: str, value: Dict[str, Any], ttl_seconds: Optional[float] = None) -> None:
        now = time.time()
        expires_at = now + ttl_seconds if ttl_seconds is not None else None
        serialized = json.dumps(value, default=str)

        with self._lock, self._get_connection() as conn:
            self._purge_expired(conn, namespace)
            conn.execute(
                """
                INSERT INTO kv_store (namespace, key, value, expires_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(namespace, key) DO UPDATE SET
                    value = excluded.value,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (namespace, key, serialized, expires_at, now),
            )
            conn.commit()

    def delete(self, namespace: str, key: str) -> bool:
        with self._lock, self._get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM kv_store WHERE namespace = ? AND key = ?",
                (namespace, key),
            )
            conn.commit()
            return cursor.rowcount > 0

    def list_keys(self, namespace: str) -> List[str]:
        now = time.time()
        with self._lock, self._get_connection() as conn:
            self._purge_expired(conn, namespace)
            cursor = conn.execute(
                """
                SELECT key FROM kv_store
                WHERE namespace = ? AND (expires_at IS NULL OR expires_at >= ?)
                ORDER BY key ASC
                """,
                (namespace, now),
            )
            return [row[0] for row in cursor.fetchall()]

    def get_all(self, namespace: str) -> Dict[str, Dict[str, Any]]:
        now = time.time()
        out: Dict[str, Dict[str, Any]] = {}
        with self._lock, self._get_connection() as conn:
            self._purge_expired(conn, namespace)
            cursor = conn.execute(
                """
                SELECT key, value FROM kv_store
                WHERE namespace = ? AND (expires_at IS NULL OR expires_at >= ?)
                """,
                (namespace, now),
            )
            for key, val_str in cursor.fetchall():
                try:
                    out[key] = json.loads(val_str)
                except Exception:
                    pass
        return out

    def clear(self, namespace: Optional[str] = None) -> None:
        with self._lock, self._get_connection() as conn:
            if namespace:
                conn.execute("DELETE FROM kv_store WHERE namespace = ?", (namespace,))
            else:
                conn.execute("DELETE FROM kv_store")
            conn.commit()

    def close(self) -> None:
        pass
