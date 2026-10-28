"""LLMGateway is the single checkpoint: redact on the way out, block leaks, restore on the way back (piece C §3.2)."""

import pytest

from hrms_plugin.ai.llm import (
    BaseLLMClient,
    ChatMessage,
    LLMConfig,
    LLMGateway,
    LLMProvider,
    LLMResponse,
    ToolCall,
)
from hrms_plugin.security.pii import PiiEgressBlocked, PiiSession

AADHAAR = "234567890124"


class EchoClient(BaseLLMClient):
    """Replies with the last message's content and echoes tool calls; records exactly what it was sent."""

    def __init__(self, tool_call_args=None):
        super().__init__(LLMConfig(provider=LLMProvider.FAKE))
        self.seen = []
        self.tool_call_args = tool_call_args

    async def chat(self, messages, tools=None, temperature=None):
        self.seen.append(messages)
        calls = [ToolCall(id="c1", name="t", arguments=self.tool_call_args)] if self.tool_call_args else []
        return LLMResponse(content=messages[-1].content, tool_calls=calls)

    async def chat_stream(self, messages, tools=None, temperature=None):
        self.seen.append(messages)
        text = messages[-1].content
        for i in range(0, len(text), 5):  # 5-char chunks split tokens across events
            yield {"event": "token", "data": text[i:i + 5]}
        yield {"event": "done", "data": {"finish_reason": "stop"}}


def gateway(client):
    gw = LLMGateway(config=LLMConfig(provider=LLMProvider.FAKE))
    gw.client = client
    return gw


def sent_text(client):
    return " ".join(m.content or "" for batch in client.seen for m in batch) + " " + " ".join(
        str(tc.arguments) for batch in client.seen for m in batch for tc in (m.tool_calls or []))


async def test_provider_sees_no_raw_pii_and_reply_is_restored():
    client = EchoClient()
    pii = PiiSession()
    pii.learn({"name": "Sunita Verma"})
    text = f"Sunita Verma, Aadhaar {AADHAAR}, mail sunita@acme.test"
    resp = await gateway(client).chat([ChatMessage(role="user", content=text)], pii=pii)
    leaked = sent_text(client)
    assert "Sunita" not in leaked and AADHAAR not in leaked and "sunita@acme.test" not in leaked
    assert resp.content == text


async def test_tool_call_arguments_are_restored_for_the_caller():
    pii = PiiSession()
    pii.learn({"candidate_name": "Deepak Patel"})
    token = pii.redact_text("Deepak Patel")
    resp = await gateway(EchoClient(tool_call_args={"candidate_name": token})).chat(
        [ChatMessage(role="user", content="evaluate Deepak Patel")], pii=pii)
    assert resp.tool_calls[0].arguments == {"candidate_name": "Deepak Patel"}


async def test_history_tool_call_arguments_are_redacted_for_the_provider():
    client = EchoClient()
    pii = PiiSession()
    history = [
        ChatMessage(
            role="assistant", content="", tool_calls=[ToolCall(id="c0", name="t", arguments={"pan": "ABCPE1234F"})]
        ),
        ChatMessage(role="tool", content="{}", tool_call_id="c0", name="t"),
        ChatMessage(role="user", content="next"),
    ]
    await gateway(client).chat(history, pii=pii)
    assert "ABCPE1234F" not in sent_text(client)


async def test_message_without_pii_is_unchanged():
    client = EchoClient()
    await gateway(client).chat([ChatMessage(role="user", content="Basic 45000, CTC 1800000")])
    assert client.seen[0][0].content == "Basic 45000, CTC 1800000"


async def test_leak_after_redaction_blocks_the_call(monkeypatch):
    monkeypatch.setattr(PiiSession, "redact_text", lambda self, text: text)
    client = EchoClient()
    with pytest.raises(PiiEgressBlocked) as info:
        await gateway(client).chat([ChatMessage(role="user", content=f"id {AADHAAR}")])
    assert info.value.kinds == ["AADHAAR"]
    assert client.seen == []


async def test_stream_leak_blocks_before_provider_call(monkeypatch):
    monkeypatch.setattr(PiiSession, "redact_text", lambda self, text: text)
    client = EchoClient()
    with pytest.raises(PiiEgressBlocked):
        [e async for e in gateway(client).chat_stream([ChatMessage(role="user", content=f"id {AADHAAR}")])]
    assert client.seen == []


async def test_stream_restores_tokens_split_across_chunks():
    client = EchoClient()
    text = f"Aadhaar {AADHAAR} confirmed for review"
    events = [e async for e in gateway(client).chat_stream([ChatMessage(role="user", content=text)])]
    assert AADHAAR not in sent_text(client)
    assert "".join(e["data"] for e in events if e["event"] == "token") == text
    assert events[-1]["event"] == "done"


async def test_calls_without_a_session_do_not_share_tokens():
    client = EchoClient()
    gw = gateway(client)
    await gw.chat([ChatMessage(role="user", content=AADHAAR)])
    await gw.chat([ChatMessage(role="user", content=AADHAAR)])
    assert client.seen[0][0].content != client.seen[1][0].content


async def test_mask_pii_switch_no_longer_exists():
    with pytest.raises(TypeError):
        await gateway(EchoClient()).chat([ChatMessage(role="user", content="x")], mask_pii=False)


def test_chat_message_to_dict_handles_decimal_arguments():                                   # agy F9
    from decimal import Decimal

    data = ChatMessage(role="assistant", content="", tool_calls=[ToolCall(id="1", name="t", arguments={
        "amount": Decimal("100.5")})]).to_dict()
    assert data["tool_calls"][0]["function"]["arguments"] == '{"amount": "100.5"}'
