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
