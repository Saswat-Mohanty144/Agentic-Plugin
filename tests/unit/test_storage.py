"""Unit tests for storage backends: SQLite, In-Memory, PII Vault, and MappingStore."""

import tempfile
import time
from pathlib import Path

from hrms_plugin.schema.canonical import EntityType
from hrms_plugin.schema.mapping import EntityMapping, FieldMap
from hrms_plugin.security.masking import PiiMaskingGateway
from hrms_plugin.storage.mapping_store import MappingStore
from hrms_plugin.storage.memory_store import MemoryKeyValueStore
from hrms_plugin.storage.sqlite_store import SqliteKeyValueStore
from hrms_plugin.storage.vault_store import PiiVaultStore


def test_memory_key_value_store():
    store = MemoryKeyValueStore()
    store.set("test_ns", "k1", {"foo": "bar"})
    assert store.get("test_ns", "k1") == {"foo": "bar"}
    assert store.list_keys("test_ns") == ["k1"]

    # TTL test
    store.set("test_ns", "expiring", {"temp": True}, ttl_seconds=0.05)
    time.sleep(0.08)
    assert store.get("test_ns", "expiring") is None

    store.delete("test_ns", "k1")
    assert store.get("test_ns", "k1") is None


def test_sqlite_key_value_store():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        store = SqliteKeyValueStore(db_path)

        store.set("ns1", "keyA", {"name": "Alice", "role": "engineer"})
        res = store.get("ns1", "keyA")
        assert res is not None
        assert res["name"] == "Alice"
        assert res["role"] == "engineer"

        # List keys
        store.set("ns1", "keyB", {"name": "Bob"})
        keys = store.list_keys("ns1")
        assert "keyA" in keys
        assert "keyB" in keys

        # Get all
        all_entries = store.get_all("ns1")
        assert len(all_entries) == 2
        assert all_entries["keyA"]["name"] == "Alice"

        # Persistence across re-open
        store2 = SqliteKeyValueStore(db_path)
        assert store2.get("ns1", "keyA") == {"name": "Alice", "role": "engineer"}

        # Delete & clear
        assert store2.delete("ns1", "keyA") is True
        assert store2.get("ns1", "keyA") is None
        store2.clear("ns1")
        assert store2.list_keys("ns1") == []


def test_pii_vault_persistence_and_gateway():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "vault.db"
        sqlite_store = SqliteKeyValueStore(db_path)
        vault_store = PiiVaultStore(sqlite_store)

        # 1. Masking gateway with vault store
        gw1 = PiiMaskingGateway(store=vault_store)
        raw_text = "Employee John with Aadhaar 9876 5432 1098 and salary 85000"
        masked1, entries = gw1.mask_text(raw_text, tenant_id="acme_corp")
        assert "9876 5432 1098" not in masked1
        assert "[VAULT_AADHAAR_" in masked1

        # 2. Simulate container restart / fresh gateway instance with same store
        gw2 = PiiMaskingGateway(store=vault_store)
        # Gateway 2 memory is completely empty
        assert len(gw2._vault) == 0

        # Gateway 2 can still unmask using persistent storage
        unmasked = gw2.unmask_text(masked1, tenant_id="acme_corp")
        assert "9876 5432 1098" in unmasked


def test_mapping_store_persistence():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "mappings.db"
        sqlite_store = SqliteKeyValueStore(db_path)
        mapping_store = MappingStore(sqlite_store)

        # Create an EntityMapping
        fm = FieldMap(
            target="full_name",
            source=("first_name", "last_name"),
            transform="join_names",
            required=True,
        )
        mapping = EntityMapping(
            vendor="workday",
            entity_type="EMPLOYEE",
            id_path="Worker_ID",
            fields=(fm,),
            writeback={"full_name": "worker.legal_name"},
        )

        mapping_store.save_mapping("tenant_123", mapping)

        # Reopen on fresh instance
        reopened_store = MappingStore(SqliteKeyValueStore(db_path))
        loaded = reopened_store.load_mapping("tenant_123", "workday", EntityType.EMPLOYEE)
        assert loaded is not None
        assert loaded.vendor == "workday"
        assert loaded.id_path == "Worker_ID"
        assert len(loaded.fields) == 1
        assert loaded.fields[0].target == "full_name"
        assert loaded.fields[0].transform == "join_names"
        assert loaded.writeback == {"full_name": "worker.legal_name"}

        mappings_list = reopened_store.list_mappings("tenant_123")
        assert len(mappings_list) == 1
        assert mappings_list[0]["vendor"] == "workday"
