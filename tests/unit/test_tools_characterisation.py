"""Snapshot of tool definitions and the archetype fallback, guarding the lint-only refactor."""

import dataclasses
import json
import urllib.error
import urllib.request
from pathlib import Path

from hrms_plugin.agents.tools import AgenticToolRegistry

SNAPSHOT = Path(__file__).parents[1] / "fixtures" / "archetypes_fallback.json"


def _current(monkeypatch) -> str:
    def refuse(*_args, **_kwargs):
        raise urllib.error.URLError("network disabled in tests")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    registry = AgenticToolRegistry()
    data = {
        "definitions": [dataclasses.asdict(d) for d in registry.get_definitions()],
        "archetypes": registry._tool_generate_archetypes(prompt="Lead Data Engineer", location="Bangalore"),
    }
    return json.dumps(data, sort_keys=True, indent=1, ensure_ascii=False)


def test_definitions_and_archetype_fallback_unchanged(monkeypatch):
    assert _current(monkeypatch) == SNAPSHOT.read_text(encoding="utf-8")


NETWORK_SNAPSHOT = Path(__file__).parents[1] / "fixtures" / "archetypes_network.json"


class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _network_current(monkeypatch) -> str:
    payload = {
        "success": True,
        "archetypes": [
            {
                "id": f"a{i}",
                "name": f"Arch {i}",
                "badge": f"Badge {i}",
                "tagline": "t",
                "jobData": {"minExperience": 3, "maxExperience": 7, "minSalary": 1500000, "maxSalary": 2500000,
                            "currency": "INR", "skills": ["Python"], "responsibilities": ["Build"]},
            }
            for i in range(4)
        ],
    }
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: _FakeResponse(payload))
    result = AgenticToolRegistry()._tool_generate_archetypes(prompt="Lead Data Engineer", location="Bangalore")
    return json.dumps(result, sort_keys=True, indent=1, ensure_ascii=False)


def test_archetype_network_path_unchanged(monkeypatch):
    assert _network_current(monkeypatch) == NETWORK_SNAPSHOT.read_text(encoding="utf-8")
