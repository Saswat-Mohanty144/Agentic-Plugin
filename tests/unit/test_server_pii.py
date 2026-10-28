"""End-to-end data protection through the HTTP API (piece C §3.4, §4 'Server')."""

import json
import os
import sqlite3
import subprocess
import sys

from hrms_plugin.agents.supervisor import SupervisorAgent
from hrms_plugin.ai.llm import BaseLLMClient, LLMConfig, LLMGateway, LLMProvider, LLMResponse
from hrms_plugin.security.pii import PiiSession

AADHAAR = "234567890124"
MESSAGE = f"Check Aadhaar {AADHAAR} for Tariq Al-Mansoor"
CONTEXT = {"employees": [{"id": "EMP-1", "name": "Tariq Al-Mansoor", "basic_salary": 4000.0}]}


class EchoClient(BaseLLMClient):
    def __init__(self):
        super().__init__(LLMConfig(provider=LLMProvider.FAKE))
        self.seen = []

    async def chat(self, messages, tools=None, temperature=None):
        self.seen.append(messages)
        return LLMResponse(content=messages[-1].content)

    async def chat_stream(self, messages, tools=None, temperature=None):
        self.seen.append(messages)
        text = messages[-1].content
        for i in range(0, len(text), 4):
            yield {"event": "token", "data": text[i:i + 4]}
        yield {"event": "done", "data": {"finish_reason": "stop"}}


def echo_supervisor():
    client = EchoClient()
    gateway = LLMGateway(config=LLMConfig(provider=LLMProvider.FAKE))
    gateway.client = client
    return SupervisorAgent(llm_gateway=gateway), client


def sent(client):
    return json.dumps([[m.content for m in batch] for batch in client.seen])


def test_chat_llm_sees_no_pii_and_reply_is_restored(make_client, bearer):
    agent, client = echo_supervisor()
    res = make_client(supervisor_agent=agent).post(
        "/v1/chat", headers=bearer(), json={"message": MESSAGE, "jurisdiction": "IN", "context": CONTEXT})
    assert res.status_code == 200
    assert AADHAAR not in sent(client) and "Tariq" not in sent(client)
    assert res.json()["reply_text"] == MESSAGE


def test_mask_pii_false_is_still_protected(make_client, bearer):
    agent, client = echo_supervisor()
    res = make_client(supervisor_agent=agent).post(
        "/v1/chat", headers=bearer(), json={"message": MESSAGE, "jurisdiction": "IN", "mask_pii": False})
    assert res.status_code == 200
    assert AADHAAR not in sent(client)


def test_stream_llm_sees_no_pii_and_tokens_are_restored(make_client, bearer):
    agent, client = echo_supervisor()
    res = make_client(supervisor_agent=agent).post(
        "/v1/chat/stream", headers=bearer(), json={"message": MESSAGE, "jurisdiction": "IN", "context": CONTEXT})
    assert res.status_code == 200
    events = [json.loads(line[6:]) for line in res.text.splitlines() if line.startswith("data: ")]
    streamed = "".join(e["data"] for e in events if e["event"] == "token")
    assert AADHAAR not in sent(client) and "Tariq" not in sent(client)
    assert streamed == MESSAGE


def test_egress_block_returns_422_with_kinds_only(make_client, bearer, monkeypatch):
    monkeypatch.setattr(PiiSession, "redact_text", lambda self, text: text)
    agent, client = echo_supervisor()
    res = make_client(supervisor_agent=agent).post(
        "/v1/chat", headers=bearer(), json={"message": MESSAGE, "jurisdiction": "IN"})
    assert res.status_code == 422
    assert res.json() == {"error": "PII_EGRESS_BLOCKED", "kinds": ["AADHAAR"]}
    assert AADHAAR not in res.text
    assert client.seen == []


def test_legacy_plaintext_vault_rows_are_purged_at_startup(tmp_path):
    from hrms_plugin.storage.sqlite_store import SqliteKeyValueStore

    store = SqliteKeyValueStore(tmp_path / "plugin_storage.db")
    store.set("pii_vault", "[VAULT_AADHAAR_ABCD1234]", {"original_value": AADHAAR, "tenant_id": "acme"})
    store.set("pii_vault_lookup", "acme:abc", {"token": "[VAULT_AADHAAR_ABCD1234]"})
    store.set("mappings", "acme:zoho:EMPLOYEE", {"keep": True})

    env = {**os.environ, "HRMS_DATA_DIR": str(tmp_path)}
    code = "import hrms_plugin.server.app"
    assert subprocess.run([sys.executable, "-c", code], env=env, capture_output=True).returncode == 0

    with sqlite3.connect(tmp_path / "plugin_storage.db") as conn:
        namespaces = {row[0] for row in conn.execute("SELECT namespace FROM kv_store")}
        assert namespaces == {"mappings"}
        assert not any(AADHAAR in str(row) for row in conn.execute("SELECT * FROM kv_store"))


def test_old_masking_and_vault_modules_are_gone():
    import importlib.util

    assert importlib.util.find_spec("hrms_plugin.security.masking") is None
    assert importlib.util.find_spec("hrms_plugin.storage.vault_store") is None
