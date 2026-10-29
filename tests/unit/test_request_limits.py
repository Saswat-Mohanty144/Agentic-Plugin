"""Token waste #7: a request can no longer fill the context window.

message <= 8,000 chars (422 MESSAGE_TOO_LONG beyond); history total <= 24,000 chars, trimmed oldest-first and
reported, always starting with a user turn (owner decision 2026-10-29).
"""

import json

import pytest

from hrms_plugin.server.app import HISTORY_BUDGET_CHARS, MESSAGE_MAX_CHARS, trim_history


class Spy:
    """Supervisor stand-in recording what reaches the agent."""

    def __init__(self):
        self.calls = []

    async def process_agentic(self, **kw):
        from hrms_plugin.agents.supervisor import AgentResponse, UserIntent

        self.calls.append(kw)
        return AgentResponse(intent=UserIntent.GENERAL_HR, routed_agent="x", reply_text="ok", confidence_score=1.0)

    def process_message(self, **kw):
        self.calls.append(kw)
        from hrms_plugin.agents.supervisor import AgentResponse, UserIntent

        return AgentResponse(intent=UserIntent.GENERAL_HR, routed_agent="x", reply_text="ok", confidence_score=1.0)

    async def stream_agentic(self, **kw):
        self.calls.append(kw)
        yield {"event": "token", "data": "ok"}

    def get_orchestrator(self):
        class Orchestrator:
            class llm_gateway:
                @staticmethod
                def preflight(*_a, **_kw):
                    return None
        return Orchestrator()


def turns(n, size):
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": f"{i}" + "x" * (size - len(str(i)))}
            for i in range(n)]


# message cap -------------------------------------------------------------------------------------------------
ROUTES = [("/v1/chat", {"jurisdiction": "IN"}), ("/v1/chat/stream", {"jurisdiction": "IN"}),
          ("/api/compliance/agent/chat", {"jurisdiction": "IN"}), ("/v1/voice/synthesize", {})]


@pytest.mark.parametrize("route, extra", ROUTES)
def test_message_at_the_cap_is_accepted_and_one_over_is_refused(make_client, bearer, route, extra):
    field = "text" if "voice" in route else "message"
    client = make_client(supervisor_agent=Spy(), raise_server_exceptions=False)
    over = client.post(route, json={field: "a" * (MESSAGE_MAX_CHARS + 1), **extra}, headers=bearer(roles=["staff"]))
    assert over.status_code == 422 and over.json() == {"error": "MESSAGE_TOO_LONG", "max_chars": MESSAGE_MAX_CHARS}
    at = client.post(route, json={field: "a" * MESSAGE_MAX_CHARS, **extra}, headers=bearer(roles=["staff"]))
    assert at.status_code != 422


# history trimming --------------------------------------------------------------------------------------------
def test_history_within_budget_is_untouched():
    history = turns(4, 1000)
    assert trim_history(history) == (history, 0)


def test_oversize_history_drops_oldest_first_and_starts_with_user():
    history = turns(50, 1000)  # 50,000 chars
    kept, dropped = trim_history(history)
    assert sum(len(h["content"]) for h in kept) <= HISTORY_BUDGET_CHARS
    assert kept[0]["role"] == "user" and kept == history[dropped:] and dropped == 26


def test_a_single_turn_over_budget_is_dropped_entirely():
    kept, dropped = trim_history([{"role": "user", "content": "x" * (HISTORY_BUDGET_CHARS + 1)}])
    assert kept == [] and dropped == 1


def test_history_exactly_at_budget_is_kept():
    history = turns(24, 1000)
    assert trim_history(history) == (history, 0)


def test_chat_route_sends_trimmed_history_and_reports_it(make_client, bearer):
    spy = Spy()
    res = make_client(supervisor_agent=spy).post(
        "/v1/chat", json={"message": "hi", "history": turns(50, 1000)}, headers=bearer(roles=["staff"]))
    sent = spy.calls[0]["history"]
    assert res.status_code == 200 and res.json()["history_trimmed"] == 26
    assert sum(len(h["content"]) for h in sent) <= HISTORY_BUDGET_CHARS and sent[0]["role"] == "user"


def test_chat_route_reports_zero_when_nothing_trimmed(make_client, bearer):
    res = make_client(supervisor_agent=Spy()).post("/v1/chat", json={"message": "hi"}, headers=bearer(roles=["staff"]))
    assert res.json()["history_trimmed"] == 0


def test_stream_reports_trimming_as_its_first_event(make_client, bearer):
    spy = Spy()
    res = make_client(supervisor_agent=spy).post(
        "/v1/chat/stream", json={"message": "hi", "history": turns(50, 1000)}, headers=bearer(roles=["staff"]))
    first = json.loads(res.text.split("\n\n")[0][len("data: "):])
    assert first == {"event": "history_trimmed", "data": 26}
    assert sum(len(h["content"]) for h in spy.calls[0]["history"]) <= HISTORY_BUDGET_CHARS
