"""The generate_archetypes tool runs in-process: no outbound call, blank prompts are a tool error (E2E review)."""

import json
import urllib.request
from pathlib import Path

import pytest

from hrms_plugin.agents.tools import AgenticToolRegistry

SNAPSHOT = Path(__file__).parents[1] / "fixtures" / "archetypes_fallback.json"


@pytest.fixture
def no_network(monkeypatch):
    calls = []

    def record(*args, **kwargs):
        calls.append(args)
        raise AssertionError("the archetype tool must not make an outbound call")

    monkeypatch.setattr(urllib.request, "urlopen", record)
    return calls


def test_tool_makes_no_outbound_call(no_network):
    result = AgenticToolRegistry()._tool_generate_archetypes(prompt="Lead Data Engineer", location="Bangalore")
    assert no_network == [] and result["success"] is True
    assert result == json.loads(SNAPSHOT.read_text(encoding="utf-8"))["archetypes"]  # behaviour production already had


@pytest.mark.parametrize("prompt", ["", " ", "\n\t "])
def test_blank_prompt_is_a_tool_error(no_network, prompt):
    assert AgenticToolRegistry()._tool_generate_archetypes(prompt=prompt) == {"success": False, "error": "EMPTY_PROMPT"}


@pytest.mark.parametrize("prompt, role", [("a", "a"), ("  data engineer  ", "Lead Data Engineer"), ("Nurse", "Nurse")])
def test_shortest_and_padded_prompts_still_work(no_network, prompt, role):
    result = AgenticToolRegistry()._tool_generate_archetypes(prompt=prompt)
    assert result["success"] is True and result["archetypes"][0]["name"].startswith(role)


async def test_blank_prompt_through_the_dispatcher_is_not_an_exception(no_network):
    out = await AgenticToolRegistry().execute("generate_requisition_archetypes", {"prompt": "  "})
    assert out == {"success": False, "error": "EMPTY_PROMPT"}
