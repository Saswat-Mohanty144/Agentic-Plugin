# Stabilise (A) and Authenticate (B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the service build, boot and pass CI, then require a host-signed token and a declared permission on every request except `/healthz`.

**Architecture:**
- Piece A fixes the entrypoint, deploy paths and lint, and corrects how the orchestrator reports intent and injects trusted arguments.
- Piece B adds a YAML tenant registry, a JWKS cache and a token verifier, exposed as FastAPI dependencies on every route. Tools and keyword-routed agents get the same permission check, so the LLM cannot reach what the user may not.
- An app factory (`create_app`) lets tests build the real app with an in-memory JWKS and a fixed clock.

**Tech Stack:** Python 3.11/3.12, FastAPI, Pydantic v2, PyJWT[crypto] 2.10.1, httpx, pytest + pytest-asyncio (auto mode), ruff.

**Spec:** `docs/superpowers/specs/2026-10-28-stabilise-and-auth-design.md` (including §9 amendments AM1–AM6). Section references below (§x.y) point to the spec.

---

## Ground rules for every task

1. **TDD (spec §2.1).** Write the test, run it, and see it fail **for the expected reason** before writing code. Copy the failing output, trimmed to the assertion lines, into `docs/superpowers/plans/2026-10-28-tdd-log.md` under the task heading, then do the same for the passing run.
2. Every behaviour has **positive, negative and boundary** tests. The test code in each task already contains all three; do not drop any.
3. After each green step, run the **full suite**: `.venv/bin/python -m pytest -q`.
   - Until Task 12 the only allowed failures are the 2 known ones (`test_chat_endpoint_payroll_routing`, `test_e2e_compliance_audit_and_remediation_lifecycle`).
   - Any other failure means stop and fix before continuing.
4. Commit after each task with the message shown, ending with the co-author trailer:
   ```
   Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
   ```
5. Never weaken an existing assertion to make it pass. If an existing test conflicts with the spec, stop and report.

## Environment (once)

```bash
cd /Users/satosh/Agentic-Plugin-main
uv venv -q -p python3.11 .venv
uv pip install -q -p .venv/bin/python -e '.[dev]'
.venv/bin/python -m pytest -q    # expect: 2 failed, 123 passed, 1 skipped
```

## File map

| File | Status | Responsibility |
|---|---|---|
| `tests/conftest.py` | new | Test env vars (data dir, fake LLM, registry); shared fixtures |
| `tests/support/__init__.py` | new | Package marker |
| `tests/support/scripted_llm.py` | new | Deterministic LLM double |
| `tests/support/auth.py` | new | Test keys, JWK export, token minting/forging, fake JWKS fetcher, clock |
| `tests/fixtures/tenant_registry.yaml` | new | Registry used by tests and the container smoke job |
| `tests/fixtures/archetypes_fallback.json` | new | Characterisation snapshot for the lint refactor |
| `hrms_plugin/server/__main__.py` | new | Entrypoint: validate env, run uvicorn |
| `hrms_plugin/server/__init__.py` | modify | Lazy exports so importing `mcp_server` does not build the app |
| `hrms_plugin/security/tenant_registry.py` | new | YAML registry model and loader |
| `hrms_plugin/security/jwks_cache.py` | new | JWKS fetch, parse, cache, outage policy |
| `hrms_plugin/security/token_verifier.py` | new | JWT validation into an `AuthContext` |
| `hrms_plugin/security/http_auth.py` | new | `authenticate`, `require`, `ApiError` |
| `hrms_plugin/security/rbac.py` | modify | Roles set, new permissions, `PermissionDenied` |
| `hrms_plugin/agents/tools.py` | modify | Lint; intent and permission per tool; required `jurisdiction` |
| `hrms_plugin/agents/orchestrator.py` | modify | Shared tool-dispatch helper: intent, trusted args, tool permissions |
| `hrms_plugin/agents/supervisor.py` | modify | `RECRUITMENT_REQUISITION`; permission check in `process_message`; `auth` passed through |
| `hrms_plugin/server/app.py` | modify | `create_app`, router, CORS, docs flag, per-route permissions, request models |
| `deploy/Dockerfile`, `deploy/docker-compose.yml`, `deploy/k8s/deployment.yaml` | modify | Non-root user, data dir, `/healthz` |
| `.github/workflows/ci.yml` | modify | `container-smoke` job |
| `pyproject.toml` | modify | `PyJWT[crypto]==2.10.1` |

---

### Task 1: Test infrastructure and scripted LLM (spec A6)

**Files:**
- Create: `tests/conftest.py`, `tests/support/__init__.py`, `tests/support/scripted_llm.py`
- Test: `tests/unit/test_scripted_llm.py`

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_scripted_llm.py`:
```python
"""Tests for the deterministic LLM double used by orchestrator and route tests."""

import pytest

from hrms_plugin.ai.llm import ChatMessage
from tests.support.scripted_llm import ScriptedLLMClient, assert_consumed, final, tool_call

MSG = [ChatMessage(role="user", content="hi")]


async def test_returns_scripted_responses_in_order():
    client = ScriptedLLMClient([tool_call("search_labor_regulations", query="gratuity"), final("done")])
    first = await client.chat(MSG)
    second = await client.chat(MSG)
    assert first.tool_calls[0].name == "search_labor_regulations"
    assert first.tool_calls[0].arguments == {"query": "gratuity"}
    assert second.content == "done"
    assert client.remaining == 0
    assert len(client.calls) == 2


async def test_records_tools_passed_on_each_call():
    client = ScriptedLLMClient([final("ok")])
    await client.chat(MSG, tools=None)
    assert client.calls[0]["tools"] is None


async def test_extra_call_raises():
    client = ScriptedLLMClient([final("only")])
    await client.chat(MSG)
    with pytest.raises(AssertionError, match="unscripted call #2"):
        await client.chat(MSG)


async def test_empty_script_fails_on_first_call():
    with pytest.raises(AssertionError, match="unscripted call #1"):
        await ScriptedLLMClient([]).chat(MSG)


def test_leftover_responses_fail_consumption_check():
    with pytest.raises(AssertionError, match="1 scripted response"):
        assert_consumed([ScriptedLLMClient([final("unused")])])


def test_consumption_check_passes_when_all_used():
    assert_consumed([ScriptedLLMClient([])])


async def test_chat_stream_emits_tokens_then_done():
    client = ScriptedLLMClient([final("a b")])
    events = [e async for e in client.chat_stream(MSG)]
    assert events == [
        {"event": "token", "data": "a "},
        {"event": "token", "data": "b "},
        {"event": "done", "data": {"finish_reason": "stop"}},
    ]
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_scripted_llm.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'tests.support'`.

- [ ] **Step 3: Implement**

`tests/support/__init__.py`:
```python
"""Shared test helpers."""
```

`tests/support/scripted_llm.py`:
```python
"""Deterministic LLM double: returns scripted responses in order and fails on surplus or leftover calls."""

from __future__ import annotations

import uuid
from typing import Any, AsyncIterator, Dict, Iterable, List, Optional, Sequence, Tuple

from hrms_plugin.ai.llm import (
    BaseLLMClient,
    ChatMessage,
    LLMConfig,
    LLMGateway,
    LLMProvider,
    LLMResponse,
    ToolCall,
    ToolDefinition,
)


class ScriptedLLMClient(BaseLLMClient):
    def __init__(self, responses: Sequence[LLMResponse]):
        super().__init__(LLMConfig(provider=LLMProvider.FAKE, model="scripted"))
        self._responses: List[LLMResponse] = list(responses)
        self.calls: List[Dict[str, Any]] = []

    @property
    def remaining(self) -> int:
        return len(self._responses)

    async def chat(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[ToolDefinition]] = None,
        temperature: Optional[float] = None,
    ) -> LLMResponse:
        self.calls.append({"messages": list(messages), "tools": tools})
        if not self._responses:
            raise AssertionError(f"ScriptedLLMClient received unscripted call #{len(self.calls)}")
        return self._responses.pop(0)

    async def chat_stream(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[ToolDefinition]] = None,
        temperature: Optional[float] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        response = await self.chat(messages, tools, temperature)
        for word in response.content.split(" ") if response.content else []:
            yield {"event": "token", "data": word + " "}
        yield {"event": "done", "data": {"finish_reason": response.finish_reason}}


def tool_call(name: str, **arguments: Any) -> LLMResponse:
    return tool_calls((name, arguments))


def tool_calls(*calls: Tuple[str, Dict[str, Any]]) -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[ToolCall(id=f"call_{uuid.uuid4().hex[:8]}", name=n, arguments=a) for n, a in calls],
        finish_reason="tool_calls",
        model="scripted",
        provider="scripted",
    )


def final(text: str) -> LLMResponse:
    return LLMResponse(content=text, finish_reason="stop", model="scripted", provider="scripted")


def scripted_gateway(responses: Iterable[LLMResponse]) -> Tuple[LLMGateway, ScriptedLLMClient]:
    gateway = LLMGateway(config=LLMConfig(provider=LLMProvider.FAKE))
    client = ScriptedLLMClient(list(responses))
    gateway.client = client
    return gateway, client


def assert_consumed(clients: Iterable[ScriptedLLMClient]) -> None:
    leftovers = sum(c.remaining for c in clients)
    if leftovers:
        raise AssertionError(f"{leftovers} scripted response(s) left unused")
```

`tests/conftest.py`:
```python
"""Test-wide environment and fixtures. Environment is set before any hrms_plugin module is imported."""

import os
import tempfile

import pytest

from tests.support.scripted_llm import assert_consumed, scripted_gateway

# Never let tests reach a paid LLM: the gateway reads these at construction time.
os.environ["LLM_PROVIDER"] = "fake"
# Keep the SQLite store out of the repository.
os.environ["HRMS_DATA_DIR"] = tempfile.mkdtemp(prefix="hrms-test-data-")


@pytest.fixture
def scripted():
    """Factory for scripted gateways; fails the test on teardown if any scripted response is unused."""
    clients = []

    def _make(*responses):
        gateway, client = scripted_gateway(responses)
        clients.append(client)
        return gateway, client

    yield _make
    assert_consumed(clients)
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_scripted_llm.py -q` → `7 passed`. Then the full suite: only the 2 known failures.

- [ ] **Step 5: Commit** — `test: add scripted LLM double and shared test environment`

---

### Task 2: Lint without behaviour change (spec A3)

This task is a refactor, so it uses a **characterisation test** (a snapshot of current behaviour) instead of red→green. The snapshot proves the line wrapping changes nothing.

**Files:**
- Modify: `hrms_plugin/agents/tools.py` (lines reported by ruff: 355, 599, 600, 617, 631, 640, 654, 663, 677)
- Create: `tests/fixtures/archetypes_fallback.json`
- Test: `tests/unit/test_tools_characterisation.py`

- [ ] **Step 1: Write the characterisation test**

```python
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
```

- [ ] **Step 2: Record the snapshot from the current, unmodified code**

```bash
mkdir -p tests/fixtures
.venv/bin/python - <<'EOF'
import pytest, sys
sys.path.insert(0, ".")
from _pytest.monkeypatch import MonkeyPatch
from tests.unit.test_tools_characterisation import _current, SNAPSHOT
mp = MonkeyPatch(); SNAPSHOT.write_text(_current(mp), encoding="utf-8"); mp.undo()
EOF
.venv/bin/python -m pytest tests/unit/test_tools_characterisation.py -q   # 1 passed
```

- [ ] **Step 3: Fix lint**
  - Run `.venv/bin/ruff check . --fix` to fix the 2 F541 errors (f-strings without placeholders).
  - For each remaining E501 line in `tools.py`, split the string literal by implicit concatenation inside the existing parentheses, for example:
    ```python
    "description": (
        "Natural language hiring query e.g. "
        "'Lead Data Engineer in Bangalore with budget 35-45 LPA'"
    ),
    ```
  - Change no characters inside the strings.

- [ ] **Step 4: Verify**

`.venv/bin/ruff check .` → `All checks passed!`. The characterisation test → `1 passed`. Full suite → only the 2 known failures.

> Task 6 changes tool signatures but not definitions or the archetype output, so this snapshot stays valid. Task 5 changes nothing that affects it either.

- [ ] **Step 5: Commit** — `style: fix ruff errors in tools.py (no behaviour change)`

---

### Task 3: Server entrypoint, data directory and lazy package exports (spec A1)

**Files:**
- Create: `hrms_plugin/server/__main__.py`
- Modify: `hrms_plugin/server/__init__.py`, `hrms_plugin/server/app.py:68`
- Test: `tests/unit/test_server_main.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for `python -m hrms_plugin.server` startup validation."""

import os
import stat
import subprocess
import sys

import pytest
import uvicorn

from hrms_plugin.server import __main__ as entry


@pytest.fixture
def ran(monkeypatch):
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append(kw))
    return calls


def test_defaults_bind_localhost_8000(ran, tmp_path):
    assert entry.main({"HRMS_DATA_DIR": str(tmp_path / "d")}) == 0
    assert ran == [{"host": "127.0.0.1", "port": 8000}]


def test_host_and_port_passed_through(ran, tmp_path):
    assert entry.main({"HOST": "0.0.0.0", "PORT": "9000", "HRMS_DATA_DIR": str(tmp_path)}) == 0
    assert ran == [{"host": "0.0.0.0", "port": 9000}]


@pytest.mark.parametrize("port", ["1", "65535"])
def test_port_boundaries_accepted(ran, tmp_path, port):
    assert entry.main({"PORT": port, "HRMS_DATA_DIR": str(tmp_path)}) == 0
    assert ran[0]["port"] == int(port)


@pytest.mark.parametrize("port", ["0", "65536", "-1", "abc", "", "8000.5", " 80"])
def test_invalid_port_exits_without_running(ran, tmp_path, capsys, port):
    assert entry.main({"PORT": port, "HRMS_DATA_DIR": str(tmp_path)}) != 0
    assert "PORT" in capsys.readouterr().err
    assert ran == []


def test_missing_data_dir_is_created_with_0700(ran, tmp_path):
    target = tmp_path / "new" / "data"
    assert entry.main({"HRMS_DATA_DIR": str(target)}) == 0
    assert target.is_dir()
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_read_only_data_dir_exits(ran, tmp_path, capsys):
    target = tmp_path / "ro"
    target.mkdir()
    target.chmod(0o500)
    try:
        assert entry.main({"HRMS_DATA_DIR": str(target)}) != 0
        assert str(target) in capsys.readouterr().err
        assert ran == []
    finally:
        target.chmod(0o700)


def test_data_dir_that_is_a_file_exits(ran, tmp_path):
    target = tmp_path / "file"
    target.write_text("x")
    assert entry.main({"HRMS_DATA_DIR": str(target)}) != 0
    assert ran == []


def test_importing_mcp_server_does_not_build_the_app():
    env = {k: v for k, v in os.environ.items() if k != "HRMS_TENANT_REGISTRY"}
    code = "import hrms_plugin.server.mcp_server, sys; sys.exit('hrms_plugin.server.app' in sys.modules)"
    assert subprocess.run([sys.executable, "-c", code], env=env).returncode == 0
```

> `test_read_only_data_dir_exits` relies on not running as root; GitHub runners and local macOS accounts are not root.

- [ ] **Step 2: Run to verify it fails**

`.venv/bin/python -m pytest tests/unit/test_server_main.py -q` → `ImportError: cannot import name '__main__'`. The last test fails because `__init__` imports `app` eagerly.

- [ ] **Step 3: Implement**

`hrms_plugin/server/__main__.py`:
```python
"""Run the HRMS plugin HTTP server: `python -m hrms_plugin.server`."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping, Optional


def _parse_port(raw: str) -> int:
    if not raw.isdigit():
        raise ValueError(raw)
    port = int(raw)
    if not 1 <= port <= 65535:
        raise ValueError(raw)
    return port


def _ensure_data_dir(raw: str) -> Path:
    path = Path(raw)
    if path.exists() and not path.is_dir():
        raise ValueError(f"HRMS_DATA_DIR {path} exists and is not a directory")
    if not path.exists():
        path.mkdir(parents=True)
        path.chmod(0o700)
    if not os.access(path, os.W_OK | os.X_OK):
        raise ValueError(f"HRMS_DATA_DIR {path} is not writable")
    return path


def main(env: Optional[Mapping[str, str]] = None) -> int:
    env = os.environ if env is None else env
    host = env.get("HOST", "127.0.0.1")
    raw_port = env.get("PORT", "8000")
    try:
        port = _parse_port(raw_port)
    except ValueError:
        print(f"Invalid PORT {raw_port!r}: must be an integer between 1 and 65535", file=sys.stderr)
        return 2
    try:
        _ensure_data_dir(env.get("HRMS_DATA_DIR", ".hrms_data"))
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    import uvicorn

    from hrms_plugin.server.app import app

    uvicorn.run(app, host=host, port=port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

`hrms_plugin/server/__init__.py`:
```python
"""Server and MCP interface package for the Standalone HRMS Plugin.

Exports are lazy: importing `hrms_plugin.server.mcp_server` must not build the HTTP app,
which requires a tenant registry.
"""

from typing import Any

__all__ = ["app", "HrmsMcpServer"]


def __getattr__(name: str) -> Any:
    if name == "app":
        from hrms_plugin.server.app import app

        return app
    if name == "HrmsMcpServer":
        from hrms_plugin.server.mcp_server import HrmsMcpServer

        return HrmsMcpServer
    raise AttributeError(name)
```

`hrms_plugin/server/app.py`: add `import os` and `from pathlib import Path`, and replace line 68 with:
```python
_storage = SqliteKeyValueStore(str(Path(os.environ.get("HRMS_DATA_DIR", ".hrms_data")) / "plugin_storage.db"))
```
Before changing it, check that `SqliteKeyValueStore.__init__` creates missing parent directories (`grep -n "mkdir" hrms_plugin/storage/sqlite_store.py`). If it does not, stop and report; do not add a silent mkdir here.

- [ ] **Step 4: Verify** — the new tests pass (13), and the full suite shows only the 2 known failures.

- [ ] **Step 5: Commit** — `feat(server): add validated entrypoint, HRMS_DATA_DIR and lazy package exports`

---

### Task 4: Deploy paths, non-root container, CI smoke job (spec A2)

**Files:**
- Modify: `deploy/Dockerfile`, `deploy/docker-compose.yml`, `deploy/k8s/deployment.yaml:47,53`, `.github/workflows/ci.yml`
- Test: `tests/unit/test_deploy_paths.py`

- [ ] **Step 1: Write the failing test**

```python
"""Every probe or healthcheck path in deploy files must be a real GET route."""

import re
from pathlib import Path
from typing import Iterable, List
from urllib.parse import urlsplit

import yaml

from hrms_plugin.server.app import app

DEPLOY = Path(__file__).parents[2] / "deploy"
_URL_PATH = re.compile(r"https?://[^/\s'\"]+(/[^\s'\")]*)")


def get_routes() -> set:
    return {r.path for r in app.routes if "GET" in getattr(r, "methods", set())}


def compose_paths(doc: dict) -> List[str]:
    paths = []
    for service in (doc.get("services") or {}).values():
        test = (service.get("healthcheck") or {}).get("test") or []
        paths += _URL_PATH.findall(" ".join(test) if isinstance(test, list) else str(test))
    return paths


def k8s_paths(docs: Iterable[dict]) -> List[str]:
    found = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("readinessProbe", "livenessProbe", "startupProbe"):
                    path = ((value or {}).get("httpGet") or {}).get("path")
                    if path:
                        found.append(path)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for d in docs:
        walk(d)
    return found


def unknown_paths(paths: Iterable[str]) -> List[str]:
    routes = get_routes()
    return [p for p in paths if urlsplit(p).path not in routes]


def test_deploy_files_reference_real_routes():
    compose = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text())
    k8s = list(yaml.safe_load_all((DEPLOY / "k8s" / "deployment.yaml").read_text()))
    paths = compose_paths(compose) + k8s_paths(k8s)
    assert len(paths) >= 3  # compose healthcheck + k8s readiness + liveness
    assert unknown_paths(paths) == []


def test_unknown_path_is_reported():
    doc = {"services": {"api": {"healthcheck": {"test": ["CMD", "curl", "http://localhost:8000/health"]}}}}
    assert unknown_paths(compose_paths(doc)) == ["/health"]


def test_query_string_is_ignored_when_matching():
    assert unknown_paths(["/healthz?x=1"]) == []
```

- [ ] **Step 2: Run** → `test_deploy_files_reference_real_routes` fails with `['/health', '/health', '/health']`.

- [ ] **Step 3: Implement**
  - `deploy/docker-compose.yml`: in the healthcheck URL, change `http://localhost:8000/health` to `http://localhost:8000/healthz`.
  - `deploy/k8s/deployment.yaml`: change both `path: /health` to `path: /healthz`.
  - `deploy/Dockerfile`, runner stage: replace everything from `ENV PYTHONUNBUFFERED=1 \` through the final `CMD` with:
    ```dockerfile
    ENV PYTHONUNBUFFERED=1 \
        PYTHONDONTWRITEBYTECODE=1 \
        HOST=0.0.0.0 \
        PORT=8000 \
        HRMS_DATA_DIR=/var/lib/hrms

    COPY --from=builder /app/wheels /wheels
    RUN pip install --no-cache-dir /wheels/* && rm -rf /wheels \
        && useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin hrms \
        && mkdir -p /var/lib/hrms && chown hrms /var/lib/hrms && chmod 700 /var/lib/hrms

    USER 10001
    EXPOSE 8000

    CMD ["python", "-m", "hrms_plugin.server"]
    ```
  - `.github/workflows/ci.yml`: append the job below. It needs `tests/fixtures/tenant_registry.yaml`, created in Task 8, and before Task 12 the app doesn't need a registry. The job is added now and becomes meaningful after Task 12.
    ```yaml
      container-smoke:
        runs-on: ubuntu-latest
        steps:
        - uses: actions/checkout@v4
        - name: Build image
          run: docker build -f deploy/Dockerfile -t hrms-plugin:ci .
        - name: Run container
          run: |
            docker run -d --name hrms -p 8000:8000 \
              -e HRMS_TENANT_REGISTRY=/etc/hrms/tenants.yaml \
              -v "$PWD/tests/fixtures/tenant_registry.yaml:/etc/hrms/tenants.yaml:ro" \
              hrms-plugin:ci
        - name: Healthz within 30 s
          run: |
            for i in $(seq 1 30); do
              if curl -fsS http://localhost:8000/healthz | grep -q '"status":"healthy"'; then exit 0; fi
              sleep 1
            done
            docker logs hrms; exit 1
        - name: Runs as non-root
          run: test "$(docker exec hrms id -u)" = "10001"
        - name: Remediation without token is rejected
          run: |
            code=$(curl -s -o /dev/null -w '%{http_code}' -X POST http://localhost:8000/v1/remediate/execute \
              -H 'Content-Type: application/json' -d '{}')
            test "$code" = "401"
    ```

- [ ] **Step 4: Verify** — the deploy-path tests pass (3). If Docker is available locally, also run `docker build -f deploy/Dockerfile -t hrms-plugin:local .`. If not, record "docker unavailable locally; verified by CI job" in the TDD log.

- [ ] **Step 5: Commit** — `fix(deploy): probe /healthz, run as non-root, add container smoke CI job`

---

### Task 5: Tool intents and `RECRUITMENT_REQUISITION` (spec A4, part 1)

**Files:**
- Modify: `hrms_plugin/agents/supervisor.py:20-32` (enum), `hrms_plugin/agents/tools.py` (`register` and its 14 call sites)
- Test: `tests/unit/test_tool_intents.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Each tool maps to exactly one intent (spec A4)."""

import pytest

from hrms_plugin.agents.supervisor import UserIntent
from hrms_plugin.agents.tools import AgenticToolRegistry
from hrms_plugin.ai.llm import ToolDefinition

EXPECTED = {
    "calculate_salary_structure": UserIntent.PAYROLL_STRUCTURE,
    "calculate_eosb_gratuity": UserIntent.PAYROLL_EOSB,
    "audit_statutory_compliance": UserIntent.COMPLIANCE_AUDIT,
    "search_labor_regulations": UserIntent.LEGAL_QNA,
    "evaluate_leave_request": UserIntent.LEAVE_APPLY,
    "detect_travel_anomaly": UserIntent.ATTENDANCE_ANOMALY,
    "score_candidate_ats": UserIntent.RECRUITMENT_ATS,
    "audit_job_description_bias": UserIntent.RECRUITMENT_BIAS,
    "evaluate_onboarding_offer": UserIntent.ONBOARDING,
    "verify_onboarding_documents": UserIntent.ONBOARDING,
    "audit_offboarding_fnf_timeline": UserIntent.OFFBOARDING_FNF,
    "compute_final_settlement": UserIntent.OFFBOARDING_FNF,
    "audit_travel_expense_claim": UserIntent.TRAVEL_EXPENSE,
    "generate_requisition_archetypes": UserIntent.RECRUITMENT_REQUISITION,
}


def test_registered_tools_are_exactly_the_expected_set():
    names = {d.name for d in AgenticToolRegistry().get_definitions()}
    assert names == set(EXPECTED)


@pytest.mark.parametrize("tool,intent", sorted(EXPECTED.items()))
def test_tool_intent(tool, intent):
    assert AgenticToolRegistry().intent_for(tool) == intent


def test_unknown_tool_intent_is_general_hr():
    assert AgenticToolRegistry().intent_for("no_such_tool") == UserIntent.GENERAL_HR


def test_register_without_intent_raises():
    registry = AgenticToolRegistry()
    with pytest.raises(TypeError):
        registry.register(ToolDefinition(name="x", description="x", parameters={}), lambda: {})


def test_get_definition_returns_none_for_unknown():
    assert AgenticToolRegistry().get_definition("no_such_tool") is None
```

- [ ] **Step 2: Run** → `AttributeError: RECRUITMENT_REQUISITION`.

- [ ] **Step 3: Implement**
  - `supervisor.py`: in `UserIntent`, after `RECRUITMENT_BIAS = "RECRUITMENT_BIAS"`, add `RECRUITMENT_REQUISITION = "RECRUITMENT_REQUISITION"`.
  - `tools.py`: add the import `from hrms_plugin.agents.supervisor import UserIntent`. In `__init__`, before `self._register_default_tools()`, add `self._intents: Dict[str, UserIntent] = {}`. Replace `register` and add two methods:
    ```python
    def register(self, definition: ToolDefinition, handler: Callable[..., Any], *, intent: UserIntent) -> None:
        self._tools[definition.name] = definition
        self._handlers[definition.name] = handler
        self._intents[definition.name] = intent

    def get_definition(self, name: str) -> Optional[ToolDefinition]:
        return self._tools.get(name)

    def intent_for(self, name: str) -> UserIntent:
        return self._intents.get(name, UserIntent.GENERAL_HR)
    ```
  - Each of the 14 `self.register(ToolDefinition(...), self._tool_x)` calls in `_register_default_tools` gains `, intent=UserIntent.<VALUE>` after the handler, using the `EXPECTED` table above.
  - If the import creates a cycle (`ImportError` at collection), stop and report; do not move the enum.

- [ ] **Step 4: Verify** — tests pass (18), and the full suite shows only the 2 known failures.

- [ ] **Step 5: Commit** — `feat(agents): map every tool to an intent; add RECRUITMENT_REQUISITION`

---

### Task 6: Orchestrator — intent from tools, trusted arguments, required jurisdiction (spec A4 part 2, A5)

**Files:**
- Modify: `hrms_plugin/agents/orchestrator.py` (rewrite of `run`/`run_stream` around a shared helper), `hrms_plugin/agents/tools.py` (6 handler signatures)
- Test: `tests/unit/test_orchestrator_dispatch.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Orchestrator dispatch: intent from executed tools, trusted argument injection, unknown tools (spec A4/A5)."""

import inspect

import pytest

from hrms_plugin.agents.orchestrator import AgenticOrchestrator, build_trusted_arguments
from hrms_plugin.agents.supervisor import UserIntent
from hrms_plugin.agents.tools import AgenticToolRegistry
from hrms_plugin.ai.llm import ToolDefinition
from tests.support.scripted_llm import final, tool_call, tool_calls
from tests.unit.test_tool_intents import EXPECTED

EMPLOYEE = {"id": "EMP-901", "basic_salary": 4000.0, "gross_salary": 10000.0, "probation_days": 210,
            "weekly_hours": 54, "uae_wps_registered": False}


def _orchestrator(gateway, max_iterations=5):
    return AgenticOrchestrator(llm_gateway=gateway, tool_registry=AgenticToolRegistry(), max_iterations=max_iterations)


async def _run(orch, **kw):
    kw.setdefault("jurisdiction", "AE")
    kw.setdefault("tenant_id", "acme")
    return await orch.run(user_message="go", **kw)


async def _stream(orch, **kw):
    kw.setdefault("jurisdiction", "AE")
    kw.setdefault("tenant_id", "acme")
    return [e async for e in orch.run_stream(user_message="go", **kw)]


# A4 — intent ----------------------------------------------------------------

@pytest.mark.parametrize("tool", sorted(EXPECTED))
async def test_intent_is_the_executed_tools_intent(scripted, tool):
    gateway, _ = scripted(tool_call(tool), final("ok"))
    resp = await _run(_orchestrator(gateway))
    assert resp.intent == EXPECTED[tool]


async def test_no_tool_calls_gives_general_hr(scripted):
    gateway, _ = scripted(final("hello"))
    assert (await _run(_orchestrator(gateway))).intent == UserIntent.GENERAL_HR


async def test_first_executed_tool_wins(scripted):
    gateway, _ = scripted(
        tool_calls(("calculate_eosb_gratuity", {}), ("search_labor_regulations", {"query": "q"})), final("ok"))
    assert (await _run(_orchestrator(gateway))).intent == UserIntent.PAYROLL_EOSB


async def test_step_limit_still_reports_intent(scripted):
    gateway, _ = scripted(tool_call("search_labor_regulations", query="a"),
                          tool_call("search_labor_regulations", query="b"))
    resp = await _run(_orchestrator(gateway, max_iterations=2))
    assert resp.intent == UserIntent.LEGAL_QNA
    assert resp.reply_text.startswith("Completed")


async def test_unknown_tool_is_not_executed(scripted):
    gateway, _ = scripted(tool_call("drop_database"), final("ok"))
    resp = await _run(_orchestrator(gateway))
    assert resp.intent == UserIntent.GENERAL_HR
    assert resp.tool_calls[0]["name"] == "drop_database"
    assert resp.structured_data is None


async def test_unknown_tool_observation_is_reported_to_llm(scripted):
    gateway, client = scripted(tool_call("drop_database"), final("ok"))
    await _run(_orchestrator(gateway))
    tool_message = client.calls[1]["messages"][-1]
    assert tool_message.role == "tool" and "UNKNOWN_TOOL" in tool_message.content


# A5 — trusted arguments ---------------------------------------------------

async def test_missing_jurisdiction_is_injected(scripted):
    gateway, _ = scripted(tool_call("search_labor_regulations", query="gratuity"), final("ok"))
    resp = await _run(_orchestrator(gateway), jurisdiction="AE")
    assert resp.tool_calls[0]["arguments"]["jurisdiction"] == "AE"
    assert resp.tool_calls[0]["overrides"] == []


async def test_llm_jurisdiction_is_overridden_and_recorded(scripted):
    gateway, _ = scripted(tool_call("search_labor_regulations", query="gratuity", jurisdiction="IN"), final("ok"))
    resp = await _run(_orchestrator(gateway), jurisdiction="AE")
    call = resp.tool_calls[0]
    assert call["arguments"]["jurisdiction"] == "AE"
    assert call["overrides"] == [{"param": "jurisdiction", "llm_value": "IN", "trusted_value": "AE"}]


async def test_tenant_id_is_injected_from_request(scripted):
    gateway, _ = scripted(tool_call("audit_statutory_compliance", tenant_id="other"), final("ok"))
    resp = await _run(_orchestrator(gateway), tenant_id="acme", context={"employees": [EMPLOYEE]})
    assert resp.tool_calls[0]["arguments"]["tenant_id"] == "acme"


async def test_context_employees_are_injected(scripted):
    gateway, _ = scripted(tool_call("audit_statutory_compliance"), final("ok"))
    resp = await _run(_orchestrator(gateway), context={"employees": [EMPLOYEE]})
    assert resp.tool_calls[0]["arguments"]["employees"] == [EMPLOYEE]
    assert resp.structured_data["violations"]  # AE rules on this record produce violations


async def test_llm_employees_are_replaced_by_context(scripted):
    gateway, _ = scripted(tool_call("audit_statutory_compliance", employees=[{"id": "FAKE"}]), final("ok"))
    resp = await _run(_orchestrator(gateway), context={"employees": [EMPLOYEE]})
    call = resp.tool_calls[0]
    assert call["arguments"]["employees"] == [EMPLOYEE]
    assert {"param": "employees", "llm_value": [{"id": "FAKE"}], "trusted_value": [EMPLOYEE]} in call["overrides"]


async def test_context_without_employees_injects_nothing(scripted):
    gateway, _ = scripted(tool_call("audit_statutory_compliance"), final("ok"))
    resp = await _run(_orchestrator(gateway), context={})
    assert "employees" not in resp.tool_calls[0]["arguments"]


def test_tool_without_jurisdiction_param_gets_no_extra_argument():
    definition = AgenticToolRegistry().get_definition("audit_job_description_bias")
    args, overrides = build_trusted_arguments(definition, {"jd_text": "x"}, {"jurisdiction": "AE", "tenant_id": "t"})
    assert args == {"jd_text": "x"} and overrides == []


def test_build_trusted_arguments_ignores_undeclared_params():
    definition = ToolDefinition(name="t", description="", parameters={"properties": {"a": {}}})
    assert build_trusted_arguments(definition, {}, {"b": 1}) == ({}, [])


@pytest.mark.parametrize("handler", [
    "_tool_calculate_salary", "_tool_calculate_eosb", "_tool_audit_compliance", "_tool_search_laws",
    "_tool_evaluate_offer", "_tool_verify_docs", "_tool_compute_final_settlement",
])
def test_jurisdiction_is_required_on_handlers(handler):
    param = inspect.signature(getattr(AgenticToolRegistry(), handler)).parameters["jurisdiction"]
    assert param.default is inspect.Parameter.empty


def test_direct_call_without_jurisdiction_raises():
    with pytest.raises(TypeError):
        AgenticToolRegistry()._tool_search_laws(query="gratuity")


# Parity between run() and run_stream() ----------------------------------------

async def test_stream_reports_intent_and_injects_same_arguments(scripted):
    gateway, _ = scripted(tool_call("search_labor_regulations", query="q", jurisdiction="IN"), final("ok"))
    events = await _stream(_orchestrator(gateway), jurisdiction="AE")
    observation = next(e for e in events if e["event"] == "tool_observation")
    assert observation["data"]["arguments"]["jurisdiction"] == "AE"
    assert observation["data"]["overrides"] == [{"param": "jurisdiction", "llm_value": "IN", "trusted_value": "AE"}]
    assert events[-1] == {"event": "intent", "data": "LEGAL_QNA"}


async def test_stream_without_tools_reports_general_hr(scripted):
    # run_stream calls the LLM twice when no tool is used (known waste, fixed in piece E).
    gateway, _ = scripted(final("hi"), final("hi"))
    events = await _stream(_orchestrator(gateway))
    assert events[-1] == {"event": "intent", "data": "GENERAL_HR"}


async def test_stream_unknown_tool_not_executed(scripted):
    gateway, _ = scripted(tool_call("drop_database"), final("ok"))
    events = await _stream(_orchestrator(gateway))
    observation = next(e for e in events if e["event"] == "tool_observation")
    assert observation["data"]["output"] == {"error": "UNKNOWN_TOOL", "tool": "drop_database"}
    assert events[-1] == {"event": "intent", "data": "GENERAL_HR"}
```

- [ ] **Step 2: Run** → `ImportError: cannot import name 'build_trusted_arguments'`.

- [ ] **Step 3: Implement**

Handler signatures in `tools.py` (only these lines change):
```python
def _tool_calculate_salary(self, gross_or_ctc: float, jurisdiction: str) -> Dict[str, Any]:
def _tool_calculate_eosb(self, basic_wage: float, tenure_years: float, jurisdiction: str) -> Dict[str, Any]:
def _tool_audit_compliance(self, jurisdiction: str, employees: Optional[List[Dict[str, Any]]] = None, tenant_id: str = "DEFAULT") -> Dict[str, Any]:
def _tool_search_laws(self, query: str, jurisdiction: str, limit: int = 3) -> Dict[str, Any]:
# _tool_evaluate_offer: replace `jurisdiction: str = "IN",` with two lines, placed after `band_max`:
        *,
        jurisdiction: str,
# _tool_compute_final_settlement: replace `jurisdiction: str = "AE",` with:
        *,
        jurisdiction: str,
```
(`_tool_verify_docs` already requires it.)

Replace `hrms_plugin/agents/orchestrator.py` with:
```python
"""Autonomous Agentic Orchestrator implementing Multi-Turn ReAct Reasoning.

Enables the AI Plugin to execute complex, multi-step agentic workflows:
Think -> Act (Tool Call) -> Observe -> Reflect -> Act -> Final Answer
With Human-In-The-Loop proposal drafting and streaming Server-Sent Events.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from pydantic import BaseModel

from hrms_plugin.agents.supervisor import AgentResponse, UserIntent
from hrms_plugin.agents.tools import AgenticToolRegistry
from hrms_plugin.ai.llm import ChatMessage, LLMGateway, TaskClass, ToolCall, ToolDefinition

logger = logging.getLogger(__name__)

SUPER_AGENT_SYSTEM_PROMPT = (
    # unchanged: copy the existing SUPER_AGENT_SYSTEM_PROMPT string literal verbatim
)

# Request context keys that replace LLM-supplied tool arguments of the same name.
TRUSTED_CONTEXT_PARAMS: Tuple[str, ...] = ("employees",)


class AgenticExecutionStep(BaseModel):
    step_number: int
    thought: Optional[str] = None
    tool_call: Optional[Dict[str, Any]] = None
    observation: Optional[Dict[str, Any]] = None


def build_trusted_arguments(
    definition: ToolDefinition,
    llm_arguments: Dict[str, Any],
    trusted: Dict[str, Any],
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Overlay trusted request values onto LLM arguments for parameters the tool declares."""
    declared = definition.parameters.get("properties", {})
    arguments = dict(llm_arguments)
    overrides: List[Dict[str, Any]] = []
    for param, trusted_value in trusted.items():
        if param not in declared:
            continue
        if param in arguments and arguments[param] != trusted_value:
            overrides.append({"param": param, "llm_value": arguments[param], "trusted_value": trusted_value})
        arguments[param] = trusted_value
    return arguments, overrides


@dataclass
class _RunState:
    steps: List[AgenticExecutionStep] = field(default_factory=list)
    executed_tools: List[str] = field(default_factory=list)
    citations: List[str] = field(default_factory=list)
    actions: List[Dict[str, Any]] = field(default_factory=list)
    last_tool_data: Optional[Dict[str, Any]] = None


def _collect_from_output(output: Any, state: _RunState) -> None:
    if not isinstance(output, dict):
        return
    if output.get("statutory_citation"):
        state.citations.append(str(output["statutory_citation"]))
    if isinstance(output.get("statutory_citations"), list):
        state.citations.extend(output["statutory_citations"])
    if isinstance(output.get("citations"), list):
        for c in output["citations"]:
            if isinstance(c, dict) and "citation" in c:
                state.citations.append(c["citation"])
    if isinstance(output.get("violations"), list):
        for v in output["violations"]:
            if not isinstance(v, dict):
                continue
            if "statutory_citation" in v:
                state.citations.append(v["statutory_citation"])
            if v.get("remediation_patch"):
                state.actions.append(
                    {
                        "action": "REMEDIATE_PATCH",
                        "violation_id": v.get("violation_id", "VIOLATION-01"),
                        "patch": v["remediation_patch"],
                    }
                )
    if isinstance(output.get("suggested_actions"), list):
        state.actions.extend(output["suggested_actions"])
    if isinstance(output.get("actions"), list):
        state.actions.extend(output["actions"])


def _collect_from_text(text: str, state: _RunState) -> None:
    lowered = text.lower()
    if "decree" in lowered or "article" in lowered or "act" in lowered:
        for line in text.split("\n"):
            if any(k in line.lower() for k in ["article", "decree-law", "act", "section"]) and len(line) < 120:
                state.citations.append(line.strip(" -•*"))


class AgenticOrchestrator:
    """Super Agentic ReAct Engine coordinating multi-step tool reasoning and statutory compliance."""

    def __init__(
        self,
        llm_gateway: Optional[LLMGateway] = None,
        tool_registry: Optional[AgenticToolRegistry] = None,
        max_iterations: int = 5,
    ):
        self.llm_gateway = llm_gateway or LLMGateway()
        self.tool_registry = tool_registry or AgenticToolRegistry()
        self.max_iterations = max_iterations

    def _system_message(self, jurisdiction: str, tenant_id: str, ctx: Dict[str, Any]) -> ChatMessage:
        context_str = json.dumps(ctx) if ctx else "None provided"
        return ChatMessage(
            role="system",
            content=(
                f"{SUPER_AGENT_SYSTEM_PROMPT}\n"
                f"Current Jurisdiction: {jurisdiction}\n"
                f"Tenant ID: {tenant_id}\n"
                f"Context: {context_str}"
            ),
        )

    @staticmethod
    def _trusted_values(jurisdiction: str, tenant_id: str, ctx: Dict[str, Any]) -> Dict[str, Any]:
        trusted: Dict[str, Any] = {"jurisdiction": jurisdiction, "tenant_id": tenant_id}
        for name in TRUSTED_CONTEXT_PARAMS:
            if name in ctx:
                trusted[name] = ctx[name]
        return trusted

    async def _execute_tool_call(
        self,
        tc: ToolCall,
        trusted: Dict[str, Any],
        state: _RunState,
        thought: Optional[str],
        iteration: int,
    ) -> Dict[str, Any]:
        """Single dispatch path shared by run() and run_stream()."""
        definition = self.tool_registry.get_definition(tc.name)
        if definition is None:
            output: Dict[str, Any] = {"error": "UNKNOWN_TOOL", "tool": tc.name}
            arguments, overrides = dict(tc.arguments), []
        else:
            arguments, overrides = build_trusted_arguments(definition, tc.arguments, trusted)
            output = await self.tool_registry.execute(tc.name, arguments)
            state.executed_tools.append(tc.name)
            state.last_tool_data = output
            _collect_from_output(output, state)
        state.steps.append(
            AgenticExecutionStep(
                step_number=iteration,
                thought=thought,
                tool_call={"id": tc.id, "name": tc.name, "arguments": arguments, "overrides": overrides},
                observation=output,
            )
        )
        return output

    def _intent(self, state: _RunState) -> UserIntent:
        return self.tool_registry.intent_for(state.executed_tools[0]) if state.executed_tools else UserIntent.GENERAL_HR

    def _response(self, state: _RunState, reply_text: str, confidence: float) -> AgentResponse:
        return AgentResponse(
            intent=self._intent(state),
            routed_agent="AgenticBrain",
            reply_text=reply_text,
            confidence_score=confidence,
            structured_data=state.last_tool_data,
            statutory_citations=list(dict.fromkeys(state.citations)),
            suggested_actions=state.actions,
            thought_process=[s.thought for s in state.steps if s.thought],
            tool_calls=[s.tool_call for s in state.steps if s.tool_call],
        )

    async def run(
        self,
        user_message: str,
        jurisdiction: str = "IN",
        context: Optional[Dict[str, Any]] = None,
        tenant_id: str = "DEFAULT",
        history: Optional[List[Dict[str, str]]] = None,
    ) -> AgentResponse:
        """Execute autonomous ReAct agent loop until final answer is formulated."""
        ctx = context or {}
        messages: List[ChatMessage] = [self._system_message(jurisdiction, tenant_id, ctx)]
        for h in history or []:
            messages.append(ChatMessage(role=h.get("role", "user"), content=h.get("content", "")))
        messages.append(ChatMessage(role="user", content=user_message))

        tools = self.tool_registry.get_definitions()
        trusted = self._trusted_values(jurisdiction, tenant_id, ctx)
        state = _RunState()

        for iteration in range(1, self.max_iterations + 1):
            llm_resp = await self.llm_gateway.chat(
                messages=messages, tools=tools, task_class=TaskClass.AGENT_REACT, tenant_id=tenant_id
            )
            thought = llm_resp.thought_process or (
                f"Step {iteration}: Reasoning through available information." if llm_resp.tool_calls else None
            )
            if not llm_resp.tool_calls:
                _collect_from_text(llm_resp.content, state)
                return self._response(state, llm_resp.content, 0.98)

            for tc in llm_resp.tool_calls:
                output = await self._execute_tool_call(tc, trusted, state, thought, iteration)
                messages.append(ChatMessage(role="assistant", content=llm_resp.content, tool_calls=[tc]))
                messages.append(
                    ChatMessage(role="tool", content=json.dumps(output), tool_call_id=tc.id, name=tc.name)
                )

        return self._response(
            state, f"Completed {len(state.steps)} agentic reasoning steps. Review findings above.", 0.90
        )

    async def run_stream(
        self,
        user_message: str,
        jurisdiction: str = "IN",
        context: Optional[Dict[str, Any]] = None,
        tenant_id: str = "DEFAULT",
    ) -> AsyncIterator[Dict[str, Any]]:
        """Stream real-time SSE events for thoughts, tool dispatches, final tokens and the resolved intent."""
        ctx = context or {}
        tools = self.tool_registry.get_definitions()
        trusted = self._trusted_values(jurisdiction, tenant_id, ctx)
        state = _RunState()
        yield {"event": "thinking", "data": f"Analyzing request in jurisdiction [{jurisdiction}]..."}

        messages: List[ChatMessage] = [
            self._system_message(jurisdiction, tenant_id, ctx),
            ChatMessage(role="user", content=user_message),
        ]
        llm_resp = await self.llm_gateway.chat(
            messages=messages, tools=tools, task_class=TaskClass.AGENT_REACT, tenant_id=tenant_id
        )
        if llm_resp.thought_process:
            yield {"event": "thinking", "data": llm_resp.thought_process}

        if llm_resp.tool_calls:
            for tc in llm_resp.tool_calls:
                yield {"event": "tool_call", "data": {"name": tc.name, "arguments": tc.arguments}}
                output = await self._execute_tool_call(tc, trusted, state, llm_resp.thought_process, 1)
                step_call = state.steps[-1].tool_call or {}
                yield {
                    "event": "tool_observation",
                    "data": {
                        "name": tc.name,
                        "arguments": step_call.get("arguments"),
                        "overrides": step_call.get("overrides"),
                        "output": output,
                    },
                }
                messages.append(ChatMessage(role="assistant", content="", tool_calls=[tc]))
                messages.append(
                    ChatMessage(role="tool", content=json.dumps(output), tool_call_id=tc.id, name=tc.name)
                )
            yield {"event": "thinking", "data": "Synthesizing verified compliant response..."}

        async for chunk in self.llm_gateway.chat_stream(messages, tenant_id=tenant_id):
            yield chunk
        yield {"event": "intent", "data": self._intent(state).value}
```
Copy the existing `SUPER_AGENT_SYSTEM_PROMPT` literal (`orchestrator.py:22-36`) verbatim into the placeholder above before running.

- [ ] **Step 4: Verify** — the new test file passes. In the full suite, `test_agentic_llm.py::test_tool_registry_registration_and_execution` already passes `jurisdiction`, so it is unaffected; other failures beyond the 2 known ones mean stop and report.

- [ ] **Step 5: Commit** — `fix(agents): report intent from executed tools and inject trusted tool arguments`

---

### Task 7: Roles set, new permissions, `PermissionDenied` (spec §6.2, §6.4)

**Files:**
- Modify: `hrms_plugin/security/rbac.py`, `tests/unit/test_security.py` (3 `AuthContext(role=...)` calls)
- Test: `tests/unit/test_rbac_roles.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Role grants, multi-role union and PermissionDenied (spec §6.2, §6.4)."""

import pytest

from hrms_plugin.security.rbac import AuthContext, Permission as P, PermissionDenied, RbacPolicyEnforcer, UserRole as R

GRANTS = {
    R.HR_ADMIN: {P.VIEW_EMPLOYEE, P.MUTATE_EMPLOYEE, P.VIEW_SALARY, P.APPLY_LEAVE, P.APPROVE_LEAVE,
                 P.VIEW_COMPLIANCE, P.EXECUTE_REMEDIATION, P.MANAGE_RECRUITMENT, P.USE_COPILOT, P.APPROVE_EXPENSE},
    R.PAYROLL_ADMIN: {P.VIEW_EMPLOYEE, P.VIEW_SALARY, P.MUTATE_SALARY, P.RUN_PAYROLL, P.VIEW_COMPLIANCE,
                      P.EXECUTE_REMEDIATION, P.USE_COPILOT, P.APPROVE_EXPENSE},
    R.HIRING_MANAGER: {P.VIEW_EMPLOYEE, P.MANAGE_RECRUITMENT, P.USE_COPILOT},
    R.DEPARTMENT_MANAGER: {P.VIEW_EMPLOYEE, P.APPLY_LEAVE, P.APPROVE_LEAVE, P.USE_COPILOT, P.APPROVE_EXPENSE},
    R.EMPLOYEE: {P.VIEW_EMPLOYEE, P.APPLY_LEAVE, P.USE_COPILOT},
    R.SUPER_ADMIN: set(P),
}


def ctx(*roles):
    return AuthContext(user_id="u", tenant_id="acme", roles=frozenset(roles))


@pytest.mark.parametrize("role", list(R))
@pytest.mark.parametrize("perm", list(P))
def test_role_grant_matrix(role, perm):
    assert RbacPolicyEnforcer().is_authorized(ctx(role), perm) == (perm in GRANTS[role])


def test_manage_schema_is_super_admin_only():
    holders = {r for r in R if RbacPolicyEnforcer().is_authorized(ctx(r), P.MANAGE_SCHEMA)}
    assert holders == {R.SUPER_ADMIN}


def test_union_of_roles():
    assert RbacPolicyEnforcer().is_authorized(ctx(R.EMPLOYEE, R.HIRING_MANAGER), P.MANAGE_RECRUITMENT)


def test_no_roles_has_no_permissions():
    assert not any(RbacPolicyEnforcer().is_authorized(ctx(), p) for p in P)


def test_auth_context_is_immutable():
    with pytest.raises(Exception):
        ctx(R.EMPLOYEE).user_id = "x"


def test_permission_denied_carries_permission():
    err = PermissionDenied(P.VIEW_SALARY)
    assert err.permission is P.VIEW_SALARY and str(err) == "VIEW_SALARY"
```

- [ ] **Step 2: Run** → `AttributeError: USE_COPILOT`.

- [ ] **Step 3: Implement** — in `rbac.py`:
  - Add `USE_COPILOT`, `MANAGE_SCHEMA` and `APPROVE_EXPENSE` to `Permission`.
  - Add the new grants to `ROLE_PERMISSIONS` as in `GRANTS` (SUPER_ADMIN stays `set(Permission)`).
  - Replace `AuthContext` and `is_authorized`, and add `PermissionDenied`:
```python
from typing import Dict, FrozenSet, Optional, Set
from pydantic import BaseModel, ConfigDict

class AuthContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: str
    tenant_id: str
    roles: FrozenSet[UserRole] = frozenset()
    issued_at: Optional[float] = None
    token_id: Optional[str] = None
    email: Optional[str] = None


class PermissionDenied(Exception):
    """Raised when an authenticated caller lacks the permission an action requires."""

    def __init__(self, permission: Permission):
        super().__init__(permission.value)
        self.permission = permission

# in RbacPolicyEnforcer
    def is_authorized(self, auth: AuthContext, permission: Permission) -> bool:
        """True if any of the caller's roles grants the permission."""
        return any(permission in ROLE_PERMISSIONS.get(role, set()) for role in auth.roles)
```
  - `tests/unit/test_security.py`: change each `role=UserRole.X` to `roles=frozenset({UserRole.X})`. Keep the assertions unchanged.

- [ ] **Step 4: Verify** — the new tests pass (6×11 matrix + 5). `test_security.py` passes. Grep the code for other `AuthContext(` callers: `grep -rn "AuthContext(" hrms_plugin`. The only one is `server/app.py:334`, which Task 12 rewrites. Until then `/v1/remediate/execute` raises a validation error, so `test_remediation_execute_endpoint` and the e2e remediation step also fail. Record those two as **temporarily expected** in the TDD log (fixed in Task 12).

- [ ] **Step 5: Commit** — `feat(security): multi-role AuthContext, USE_COPILOT/MANAGE_SCHEMA/APPROVE_EXPENSE, PermissionDenied`

---

### Task 8: Tenant registry (spec §6.1)

**Files:**
- Create: `hrms_plugin/security/tenant_registry.py`, `tests/fixtures/tenant_registry.yaml`
- Modify: `tests/conftest.py` (registry env var)
- Test: `tests/unit/test_tenant_registry.py`

- [ ] **Step 1: Create the fixture and write the failing tests**

`tests/fixtures/tenant_registry.yaml`:
```yaml
tenants:
  - tenant_id: acme
    issuer: https://idp.acme.test
    jwks_url: https://idp.acme.test/jwks.json
    audience: hrms-agentic-plugin
    algorithms: [RS256, ES256]
    claims: {tenant: tid, roles: roles, subject: sub}
    role_map:
      platform_admin: SUPER_ADMIN
      hr_manager: HR_ADMIN
      payroll_officer: PAYROLL_ADMIN
      recruiter: HIRING_MANAGER
      line_manager: DEPARTMENT_MANAGER
      staff: EMPLOYEE
    allowed_origins: [https://hr.acme.test]
    leeway_seconds: 30
    max_token_lifetime_seconds: 3600
  - tenant_id: globex
    issuer: https://idp.acme.test
    jwks_url: https://idp.acme.test/jwks.json
    audience: hrms-agentic-plugin
    algorithms: [RS256]
    claims: {tenant: tid, roles: roles, subject: sub}
    role_map: {hr_manager: HR_ADMIN, staff: EMPLOYEE}
    allowed_origins: [https://hr.globex.test]
    leeway_seconds: 30
    max_token_lifetime_seconds: 3600
  - tenant_id: initech
    issuer: https://idp.initech.test
    jwks_url: https://idp.initech.test/jwks.json
    audience: hrms-agentic-plugin
    algorithms: [RS256]
    claims: {tenant: org, roles: groups, subject: sub}
    role_map: {hr: HR_ADMIN}
    allowed_origins: ["https://hr.initech.test:8443"]
    leeway_seconds: 0
    max_token_lifetime_seconds: 60
```

`tests/unit/test_tenant_registry.py`:
```python
"""Tenant registry validation (spec §6.1)."""

import copy
from pathlib import Path

import pytest
import yaml

from hrms_plugin.security.rbac import UserRole
from hrms_plugin.security.tenant_registry import RegistryError, load_registry, load_registry_from_env

FIXTURE = Path(__file__).parents[1] / "fixtures" / "tenant_registry.yaml"
BASE = yaml.safe_load(FIXTURE.read_text())


def write(tmp_path, data) -> Path:
    path = tmp_path / "reg.yaml"
    path.write_text(yaml.safe_dump(data) if not isinstance(data, str) else data)
    return path


def one(**overrides) -> dict:
    tenant = copy.deepcopy(BASE["tenants"][0])
    tenant.update(overrides)
    return {"tenants": [tenant]}


def test_fixture_loads():
    reg = load_registry(FIXTURE)
    assert [t.tenant_id for t in reg.tenants] == ["acme", "globex", "initech"]
    assert reg.find("https://idp.acme.test", "acme").role_map["hr_manager"] is UserRole.HR_ADMIN


def test_shared_issuer_resolves_each_tenant():
    reg = load_registry(FIXTURE)
    assert reg.find("https://idp.acme.test", "globex").tenant_id == "globex"
    assert reg.claims_for_issuer("https://idp.acme.test").tenant == "tid"


def test_lookups_for_unknown_values_return_none():
    reg = load_registry(FIXTURE)
    assert reg.find("https://idp.acme.test", "initech") is None
    assert reg.find("https://nope.test", "acme") is None
    assert reg.claims_for_issuer("https://nope.test") is None


def test_all_origins_is_sorted_union():
    assert load_registry(FIXTURE).all_origins() == [
        "https://hr.acme.test", "https://hr.globex.test", "https://hr.initech.test:8443"]


@pytest.mark.parametrize("field,value", [
    ("tenant_id", "has space"), ("tenant_id", ""), ("tenant_id", "a" * 65),
    ("issuer", ""), ("audience", ""),
    ("jwks_url", "http://idp.acme.test/jwks.json"), ("jwks_url", "/jwks.json"), ("jwks_url", "https://"),
    ("algorithms", []), ("algorithms", ["HS256"]), ("algorithms", ["none"]),
    ("claims", {"tenant": "", "roles": "roles", "subject": "sub"}),
    ("claims", {"tenant": "tid", "roles": "roles"}),
    ("role_map", {}), ("role_map", {"x": "GOD_MODE"}),
    ("allowed_origins", ["*"]), ("allowed_origins", ["http://hr.acme.test"]),
    ("allowed_origins", ["https://hr.acme.test/"]), ("allowed_origins", ["https://hr.acme.test/app"]),
    ("allowed_origins", ["https://hr.acme.test?x=1"]), ("allowed_origins", ["https://hr.acme.test:99999"]),
    ("leeway_seconds", -1), ("leeway_seconds", 121), ("leeway_seconds", "30"), ("leeway_seconds", True),
    ("max_token_lifetime_seconds", 59), ("max_token_lifetime_seconds", 86401),
])
def test_invalid_field_rejected(tmp_path, field, value):
    with pytest.raises(RegistryError):
        load_registry(write(tmp_path, one(**{field: value})))


@pytest.mark.parametrize("field,value", [
    ("tenant_id", "a" * 64), ("leeway_seconds", 0), ("leeway_seconds", 120),
    ("max_token_lifetime_seconds", 60), ("max_token_lifetime_seconds", 86400),
    ("allowed_origins", []), ("allowed_origins", ["https://hr.acme.test:8443"]),
])
def test_boundary_values_accepted(tmp_path, field, value):
    load_registry(write(tmp_path, one(**{field: value})))


def test_unknown_key_rejected(tmp_path):
    with pytest.raises(RegistryError):
        load_registry(write(tmp_path, one(extra_key=1)))


def test_unknown_top_level_key_rejected(tmp_path):
    data = one()
    data["version"] = 2
    with pytest.raises(RegistryError):
        load_registry(write(tmp_path, data))


def test_empty_tenant_list_rejected(tmp_path):
    with pytest.raises(RegistryError):
        load_registry(write(tmp_path, {"tenants": []}))


def test_duplicate_tenant_id_rejected(tmp_path):
    data = one()
    data["tenants"].append(copy.deepcopy(data["tenants"][0]))
    with pytest.raises(RegistryError, match="duplicate tenant_id"):
        load_registry(write(tmp_path, data))


def test_shared_issuer_with_different_claims_rejected(tmp_path):
    data = copy.deepcopy(BASE)
    data["tenants"][1]["claims"] = {"tenant": "org", "roles": "roles", "subject": "sub"}
    with pytest.raises(RegistryError, match="identical claims"):
        load_registry(write(tmp_path, data))


def test_invalid_yaml_rejected(tmp_path):
    with pytest.raises(RegistryError, match="YAML"):
        load_registry(write(tmp_path, "tenants: [\n"))


def test_non_mapping_document_rejected(tmp_path):
    with pytest.raises(RegistryError):
        load_registry(write(tmp_path, "- just\n- a list\n"))


def test_missing_file_rejected(tmp_path):
    with pytest.raises(RegistryError, match="cannot read"):
        load_registry(tmp_path / "missing.yaml")


def test_env_var_unset_rejected():
    with pytest.raises(RegistryError, match="HRMS_TENANT_REGISTRY"):
        load_registry_from_env({})


def test_env_var_empty_rejected():
    with pytest.raises(RegistryError, match="HRMS_TENANT_REGISTRY"):
        load_registry_from_env({"HRMS_TENANT_REGISTRY": ""})


def test_env_var_loads_file():
    assert load_registry_from_env({"HRMS_TENANT_REGISTRY": str(FIXTURE)}).tenants[0].tenant_id == "acme"


def test_registry_is_immutable():
    reg = load_registry(FIXTURE)
    with pytest.raises(Exception):
        reg.tenants[0].issuer = "x"
```

- [ ] **Step 2: Run** → `ModuleNotFoundError: hrms_plugin.security.tenant_registry`.

- [ ] **Step 3: Implement**

`hrms_plugin/security/tenant_registry.py`:
```python
"""Per-tenant token issuer registry, loaded from YAML and validated strictly at startup."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Literal, Mapping, Optional, Tuple, Union
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError, field_validator, model_validator

from hrms_plugin.security.rbac import UserRole

REGISTRY_ENV_VAR = "HRMS_TENANT_REGISTRY"


class RegistryError(Exception):
    """The tenant registry is missing or invalid; the service must not start."""


class ClaimNames(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant: StrictStr = Field(min_length=1)
    roles: StrictStr = Field(min_length=1)
    subject: StrictStr = Field(min_length=1)


class TenantConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: StrictStr = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    issuer: StrictStr = Field(min_length=1)
    jwks_url: StrictStr
    audience: StrictStr = Field(min_length=1)
    algorithms: Tuple[Literal["RS256", "ES256"], ...] = Field(min_length=1)
    claims: ClaimNames
    role_map: Dict[StrictStr, UserRole] = Field(min_length=1)
    allowed_origins: Tuple[StrictStr, ...]
    leeway_seconds: StrictInt = Field(ge=0, le=120)
    max_token_lifetime_seconds: StrictInt = Field(ge=60, le=86400)

    @field_validator("jwks_url")
    @classmethod
    def _https_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("jwks_url must be an absolute https:// URL")
        return value

    @field_validator("allowed_origins")
    @classmethod
    def _exact_origins(cls, values: Tuple[str, ...]) -> Tuple[str, ...]:
        for origin in values:
            parts = urlsplit(origin)
            try:
                parts.port
            except ValueError:
                raise ValueError(f"invalid port in origin {origin!r}") from None
            if (
                parts.scheme != "https"
                or not parts.hostname
                or parts.path
                or parts.query
                or parts.fragment
                or parts.username
            ):
                raise ValueError(f"invalid origin {origin!r}: must be https://host[:port] with nothing after it")
        return values


class TenantRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenants: Tuple[TenantConfig, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> "TenantRegistry":
        seen = set()
        claims_by_issuer: Dict[str, ClaimNames] = {}
        for tenant in self.tenants:
            if tenant.tenant_id in seen:
                raise ValueError(f"duplicate tenant_id {tenant.tenant_id!r}")
            seen.add(tenant.tenant_id)
            first = claims_by_issuer.setdefault(tenant.issuer, tenant.claims)
            if first != tenant.claims:
                raise ValueError(f"tenants sharing issuer {tenant.issuer!r} must use identical claims")
        return self

    def claims_for_issuer(self, issuer: str) -> Optional[ClaimNames]:
        for tenant in self.tenants:
            if tenant.issuer == issuer:
                return tenant.claims
        return None

    def find(self, issuer: str, tenant_id: str) -> Optional[TenantConfig]:
        for tenant in self.tenants:
            if tenant.issuer == issuer and tenant.tenant_id == tenant_id:
                return tenant
        return None

    def all_origins(self) -> List[str]:
        return sorted({origin for tenant in self.tenants for origin in tenant.allowed_origins})


def load_registry(path: Union[str, Path]) -> TenantRegistry:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise RegistryError(f"cannot read tenant registry {path}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RegistryError(f"invalid YAML in tenant registry {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RegistryError(f"tenant registry {path} must be a mapping with a 'tenants' list")
    try:
        return TenantRegistry.model_validate(data)
    except ValidationError as exc:
        raise RegistryError(f"invalid tenant registry {path}: {exc}") from exc


def load_registry_from_env(env: Optional[Mapping[str, str]] = None) -> TenantRegistry:
    env = os.environ if env is None else env
    path = env.get(REGISTRY_ENV_VAR)
    if not path:
        raise RegistryError(f"{REGISTRY_ENV_VAR} is not set; the plugin cannot authenticate requests without it")
    return load_registry(path)
```

`tests/conftest.py`: after the `HRMS_DATA_DIR` line, add:
```python
from pathlib import Path  # noqa: E402  (keep with the env block)

os.environ["HRMS_TENANT_REGISTRY"] = str(Path(__file__).parent / "fixtures" / "tenant_registry.yaml")
```
(Place the `from pathlib import Path` with the other imports at the top instead if ruff's isort rule complains.)

- [ ] **Step 4: Verify** — the registry tests pass. Check the `True` case for `leeway_seconds` explicitly: `StrictInt` rejects booleans, so it must be in the failing set. If it is accepted, stop and report.

- [ ] **Step 5: Commit** — `feat(security): YAML tenant registry with strict validation`

---

### Task 9: JWKS cache (spec §6.3, AM3, AM6)

**Files:**
- Create: `hrms_plugin/security/jwks_cache.py`, `tests/support/auth.py`
- Modify: `pyproject.toml` (dependency)
- Test: `tests/unit/test_jwks_cache.py`

- [ ] **Step 1: Add the dependency**

In `pyproject.toml` `dependencies`, add `"PyJWT[crypto]==2.10.1",`. Then run `uv pip install -q -p .venv/bin/python -e '.[dev]'`.

- [ ] **Step 2: Create the test support module** (used from here on)

`tests/support/auth.py`:
```python
"""Keys, JWKs, token minting/forging, a fake JWKS fetcher and a controllable clock for auth tests."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional, Set

import jwt
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

from hrms_plugin.security.jwks_cache import JwksFetchError

NOW = 1_800_000_000
AUDIENCE = "hrms-agentic-plugin"
ACME_ISSUER = "https://idp.acme.test"
ACME_JWKS = "https://idp.acme.test/jwks.json"
INITECH_ISSUER = "https://idp.initech.test"
INITECH_JWKS = "https://idp.initech.test/jwks.json"


@dataclass
class TestKeys:
    rsa: Any
    ec: Any
    weak_rsa: Any
    p384: Any
    initech_rsa: Any


def generate_test_keys() -> TestKeys:
    return TestKeys(
        rsa=rsa.generate_private_key(public_exponent=65537, key_size=2048),
        ec=ec.generate_private_key(ec.SECP256R1()),
        weak_rsa=rsa.generate_private_key(public_exponent=65537, key_size=1024),
        p384=ec.generate_private_key(ec.SECP384R1()),
        initech_rsa=rsa.generate_private_key(public_exponent=65537, key_size=2048),
    )


def public_jwk(private_key: Any, kid: str, *, use: Optional[str] = None) -> Dict[str, Any]:
    public = private_key.public_key()
    if isinstance(public, rsa.RSAPublicKey):
        jwk = json.loads(RSAAlgorithm.to_jwk(public))
        jwk["alg"] = "RS256"
    else:
        jwk = json.loads(ECAlgorithm.to_jwk(public))
    jwk["kid"] = kid
    if use is not None:
        jwk["use"] = use
    return jwk


def acme_jwks(keys: TestKeys, *extra: Dict[str, Any]) -> Dict[str, Any]:
    return {"keys": [public_jwk(keys.rsa, "acme-rsa"), public_jwk(keys.ec, "acme-ec"), *extra]}


def claims(
    *,
    tenant: str = "acme",
    roles: Iterable[str] = ("hr_manager",),
    sub: str = "user-1",
    iat: float = NOW,
    lifetime: float = 600,
    issuer: str = ACME_ISSUER,
    audience: Any = AUDIENCE,
    tenant_claim: str = "tid",
    roles_claim: str = "roles",
    **extra: Any,
) -> Dict[str, Any]:
    data = {"iss": issuer, "aud": audience, "sub": sub, "iat": iat, "exp": iat + lifetime,
            tenant_claim: tenant, roles_claim: list(roles), "jti": f"jti-{sub}"}
    data.update(extra)
    return data


def mint(private_key: Any, kid: str, payload: Dict[str, Any], *, alg: str = "RS256",
         headers: Optional[Dict[str, Any]] = None) -> str:
    return jwt.encode(payload, private_key, algorithm=alg, headers={"kid": kid, **(headers or {})})


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def forge(header: Dict[str, Any], payload: Dict[str, Any], signature: bytes = b"") -> str:
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(payload).encode())}"
    return f"{signing_input}.{_b64(signature)}"


def forge_hs256(payload: Dict[str, Any], secret: bytes, kid: str) -> str:
    header = {"alg": "HS256", "typ": "JWT", "kid": kid}
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(payload).encode())}"
    signature = hmac.new(secret, signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64(signature)}"


class FakeJwksFetcher:
    """In-memory JWKS source. Raises JwksFetchError for URLs in `failures`."""

    def __init__(self, documents: Dict[str, Any], *, delay: float = 0.0):
        self.documents = documents
        self.failures: Set[str] = set()
        self.calls: list = []
        self.delay = delay

    async def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        if self.delay:
            await asyncio.sleep(self.delay)
        if url in self.failures:
            raise JwksFetchError("simulated outage")
        document = self.documents[url]
        return document if isinstance(document, bytes) else json.dumps(document).encode()


class MutableClock:
    def __init__(self, now: float):
        self.now = now

    def __call__(self) -> float:
        return self.now
```

- [ ] **Step 3: Write the failing tests**

`tests/unit/test_jwks_cache.py`:
```python
"""JWKS cache freshness, rate limits, outage policy and key filtering (spec §6.3, AM3)."""

import asyncio
import json

import pytest

from hrms_plugin.security.jwks_cache import (
    AuthUnavailable,
    JwksCache,
    JwksFetchError,
    UnknownSigningKey,
    parse_jwks,
)
from tests.support.auth import ACME_JWKS, FakeJwksFetcher, MutableClock, acme_jwks, generate_test_keys, public_jwk

KEYS = generate_test_keys()


@pytest.fixture
def fetcher():
    return FakeJwksFetcher({ACME_JWKS: acme_jwks(KEYS)})


@pytest.fixture
def clock():
    return MutableClock(0.0)


@pytest.fixture
def cache(fetcher, clock):
    return JwksCache(fetcher, clock=clock)


async def test_first_lookup_fetches_second_uses_cache(cache, fetcher, clock):
    await cache.get_signing_key(ACME_JWKS, "acme-rsa")
    clock.now = 599
    await cache.get_signing_key(ACME_JWKS, "acme-ec")
    assert len(fetcher.calls) == 1


async def test_refresh_at_600_seconds(cache, fetcher, clock):
    await cache.get_signing_key(ACME_JWKS, "acme-rsa")
    clock.now = 600
    await cache.get_signing_key(ACME_JWKS, "acme-rsa")
    assert len(fetcher.calls) == 2


async def test_key_rotation_new_kid_accepted_after_refresh(cache, fetcher, clock):
    await cache.get_signing_key(ACME_JWKS, "acme-rsa")
    fetcher.documents[ACME_JWKS] = {"keys": [public_jwk(KEYS.initech_rsa, "rotated")]}
    clock.now = 10
    assert await cache.get_signing_key(ACME_JWKS, "rotated") is not None
    assert len(fetcher.calls) == 2


async def test_unknown_kid_refresh_rate_limit(cache, fetcher, clock):
    await cache.get_signing_key(ACME_JWKS, "acme-rsa")          # fetch 1 at t=0
    for now, expected_calls in [(10, 2), (20, 2), (69, 2), (70, 3)]:
        clock.now = now
        with pytest.raises(UnknownSigningKey):
            await cache.get_signing_key(ACME_JWKS, "nope")
        assert len(fetcher.calls) == expected_calls, now


async def test_unknown_kid_on_first_fetch_does_not_double_fetch(cache, fetcher):
    with pytest.raises(UnknownSigningKey):
        await cache.get_signing_key(ACME_JWKS, "nope")
    assert len(fetcher.calls) == 1


@pytest.mark.parametrize("document", [b"x" * (64 * 1024 + 1), b"not json", b'{"no_keys": []}', b"[]"])
async def test_bad_document_with_empty_cache_is_unavailable(clock, document):
    cache = JwksCache(FakeJwksFetcher({ACME_JWKS: document}), clock=clock)
    with pytest.raises(AuthUnavailable):
        await cache.get_signing_key(ACME_JWKS, "acme-rsa")


async def test_fetch_failure_with_empty_cache_is_unavailable(cache, fetcher):
    fetcher.failures.add(ACME_JWKS)
    with pytest.raises(AuthUnavailable):
        await cache.get_signing_key(ACME_JWKS, "acme-rsa")


async def test_stale_cache_used_up_to_3600_seconds_then_unavailable(cache, fetcher, clock):
    await cache.get_signing_key(ACME_JWKS, "acme-rsa")
    fetcher.failures.add(ACME_JWKS)
    clock.now = 3600
    assert await cache.get_signing_key(ACME_JWKS, "acme-rsa") is not None
    clock.now = 3601
    with pytest.raises(AuthUnavailable):
        await cache.get_signing_key(ACME_JWKS, "acme-rsa")


async def test_retry_after_failure_waits_60_seconds(cache, fetcher, clock):
    await cache.get_signing_key(ACME_JWKS, "acme-rsa")       # call 1 at t=0
    fetcher.failures.add(ACME_JWKS)
    clock.now = 600
    await cache.get_signing_key(ACME_JWKS, "acme-rsa")       # call 2 fails, cache used
    clock.now = 659
    await cache.get_signing_key(ACME_JWKS, "acme-rsa")       # skipped (59 s)
    assert len(fetcher.calls) == 2
    clock.now = 660
    await cache.get_signing_key(ACME_JWKS, "acme-rsa")       # attempted (60 s)
    assert len(fetcher.calls) == 3


async def test_only_weak_rsa_key_is_unknown(clock):
    fetcher = FakeJwksFetcher({ACME_JWKS: {"keys": [public_jwk(KEYS.weak_rsa, "weak")]}})
    with pytest.raises(UnknownSigningKey):
        await JwksCache(fetcher, clock=clock).get_signing_key(ACME_JWKS, "weak")


@pytest.mark.parametrize("jwk", [
    public_jwk(KEYS.rsa, "k", use="enc"),
    public_jwk(KEYS.p384, "k"),
    {"kty": "oct", "kid": "k", "k": "c2VjcmV0"},
])
def test_unusable_keys_are_skipped(jwk):
    assert parse_jwks(json.dumps({"keys": [jwk]}).encode()) == {}


def test_usable_keys_kept():
    keys = parse_jwks(json.dumps(acme_jwks(KEYS, public_jwk(KEYS.rsa, "sig-use", use="sig"))).encode())
    assert set(keys) == {"acme-rsa", "acme-ec", "sig-use"}


def test_keys_without_kid_are_skipped():
    jwk = public_jwk(KEYS.rsa, "x")
    del jwk["kid"]
    assert parse_jwks(json.dumps({"keys": [jwk]}).encode()) == {}


def test_document_of_exactly_64_kib_is_parsed():
    body = json.dumps({"keys": [], "pad": ""}).encode()
    body = body.replace(b'"pad": ""', b'"pad": "' + b"x" * (64 * 1024 - len(body)) + b'"')
    assert len(body) == 64 * 1024
    assert parse_jwks(body) == {}


async def test_concurrent_unknown_kid_triggers_exactly_one_fetch(clock):
    fetcher = FakeJwksFetcher({ACME_JWKS: acme_jwks(KEYS)}, delay=0.01)
    cache = JwksCache(fetcher, clock=clock)
    await cache.get_signing_key(ACME_JWKS, "acme-rsa")
    fetcher.documents[ACME_JWKS] = acme_jwks(KEYS, public_jwk(KEYS.initech_rsa, "new"))
    clock.now = 10
    results = await asyncio.gather(*[cache.get_signing_key(ACME_JWKS, "new") for _ in range(100)])
    assert all(r is not None for r in results)
    assert len(fetcher.calls) == 2


async def test_httpx_fetcher_rejects_oversized_and_non_200():
    import httpx

    from hrms_plugin.security.jwks_cache import make_httpx_fetcher

    def handler(request):
        if request.url.path == "/big":
            return httpx.Response(200, content=b"x" * (64 * 1024 + 1))
        if request.url.path == "/err":
            return httpx.Response(500)
        return httpx.Response(200, content=b'{"keys": []}')

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetch = make_httpx_fetcher(client)
        assert await fetch("https://idp.test/ok") == b'{"keys": []}'
        for path in ("/big", "/err"):
            with pytest.raises(JwksFetchError):
                await fetch(f"https://idp.test{path}")
```

- [ ] **Step 4: Run** → `ModuleNotFoundError: hrms_plugin.security.jwks_cache` (also raised from `tests/support/auth.py`).

- [ ] **Step 5: Implement**

`hrms_plugin/security/jwks_cache.py`:
```python
"""Per-URL cache of a host's JSON Web Key Set with bounded refreshes and an outage fallback."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

import httpx
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt import PyJWK
from jwt.exceptions import PyJWTError

logger = logging.getLogger("hrms_plugin.security.auth")

MAX_JWKS_BYTES = 64 * 1024
FETCH_TIMEOUT_SECONDS = 5.0
FRESH_SECONDS = 600
KID_REFRESH_INTERVAL_SECONDS = 60
FAILURE_RETRY_SECONDS = 60
MAX_STALE_SECONDS = 3600
MIN_RSA_BITS = 2048

Fetcher = Callable[[str], Awaitable[bytes]]


class JwksFetchError(Exception):
    """The JWKS document could not be fetched or is unusable."""


class AuthUnavailable(Exception):
    """No usable keys: the fetch failed and no cache is young enough to fall back on."""

    def __init__(self, jwks_url: str):
        super().__init__(jwks_url)
        self.jwks_url = jwks_url


class UnknownSigningKey(Exception):
    """No usable key with the requested `kid`."""


def make_httpx_fetcher(client: httpx.AsyncClient) -> Fetcher:
    async def fetch(url: str) -> bytes:
        try:
            async with client.stream("GET", url, timeout=FETCH_TIMEOUT_SECONDS) as response:
                if response.status_code != 200:
                    raise JwksFetchError(f"HTTP {response.status_code}")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_JWKS_BYTES:
                        raise JwksFetchError("JWKS document too large")
                return bytes(body)
        except httpx.HTTPError as exc:
            raise JwksFetchError(str(exc)) from exc

    return fetch


def _usable_key(jwk: Dict[str, Any]) -> Optional[Any]:
    if jwk.get("use") not in (None, "sig"):
        return None
    try:
        key = PyJWK(jwk).key
    except (PyJWTError, ValueError, TypeError, KeyError):
        return None
    if isinstance(key, rsa.RSAPublicKey) and key.key_size >= MIN_RSA_BITS:
        return key
    if isinstance(key, ec.EllipticCurvePublicKey) and isinstance(key.curve, ec.SECP256R1):
        return key
    return None


def parse_jwks(body: bytes) -> Dict[str, Any]:
    if len(body) > MAX_JWKS_BYTES:
        raise JwksFetchError("JWKS document too large")
    try:
        document = json.loads(body)
    except ValueError as exc:
        raise JwksFetchError("JWKS document is not JSON") from exc
    keys = document.get("keys") if isinstance(document, dict) else None
    if not isinstance(keys, list):
        raise JwksFetchError("JWKS document has no 'keys' list")
    usable: Dict[str, Any] = {}
    for jwk in keys:
        kid = jwk.get("kid") if isinstance(jwk, dict) else None
        if not isinstance(kid, str) or not kid:
            continue
        key = _usable_key(jwk)
        if key is None:
            logger.info("JWKS key skipped", extra={"reason": "KEY_REJECTED", "kid": kid})
            continue
        usable[kid] = key
    return usable


@dataclass
class _Entry:
    keys: Dict[str, Any]
    fetched_at: float


class JwksCache:
    """Keys are cached per JWKS URL (AM6). All lookups for one URL are serialised by a lock."""

    def __init__(self, fetcher: Fetcher, clock: Callable[[], float] = time.monotonic):
        self._fetcher = fetcher
        self._clock = clock
        self._entries: Dict[str, _Entry] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._last_kid_refresh: Dict[str, float] = {}
        self._last_failure: Dict[str, float] = {}

    async def get_signing_key(self, jwks_url: str, kid: str) -> Any:
        lock = self._locks.setdefault(jwks_url, asyncio.Lock())
        async with lock:
            now = self._clock()
            entry = self._entries.get(jwks_url)
            refreshed = False
            if entry is None or now - entry.fetched_at >= FRESH_SECONDS:
                entry, refreshed = await self._refresh(jwks_url, now)
            key = entry.keys.get(kid)
            if key is not None:
                return key
            last = self._last_kid_refresh.get(jwks_url)
            if not refreshed and (last is None or now - last >= KID_REFRESH_INTERVAL_SECONDS):
                self._last_kid_refresh[jwks_url] = now
                entry, _ = await self._refresh(jwks_url, now)
                key = entry.keys.get(kid)
                if key is not None:
                    return key
            raise UnknownSigningKey(kid)

    async def _refresh(self, jwks_url: str, now: float) -> Tuple[_Entry, bool]:
        cached = self._entries.get(jwks_url)
        failed_at = self._last_failure.get(jwks_url)
        if failed_at is None or now - failed_at >= FAILURE_RETRY_SECONDS:
            try:
                keys = parse_jwks(await self._fetcher(jwks_url))
            except JwksFetchError as exc:
                self._last_failure[jwks_url] = now
                logger.warning(
                    "JWKS fetch failed",
                    extra={"reason": "JWKS_FETCH_FAILED", "jwks_url": jwks_url, "error": str(exc)},
                )
            else:
                self._last_failure.pop(jwks_url, None)
                entry = _Entry(keys=keys, fetched_at=now)
                self._entries[jwks_url] = entry
                return entry, True
        if cached is not None and now - cached.fetched_at <= MAX_STALE_SECONDS:
            return cached, False
        raise AuthUnavailable(jwks_url)
```

- [ ] **Step 6: Verify** — the JWKS tests pass, and the full suite shows only the known failures (2 original + 2 temporary from Task 7).

- [ ] **Step 7: Commit** — `feat(security): JWKS cache with bounded refresh and outage fallback`

---

### Task 10: Token verifier (spec §6.2)

**Files:**
- Create: `hrms_plugin/security/token_verifier.py`
- Modify: `tests/conftest.py` (auth fixtures)
- Test: `tests/unit/test_token_verifier.py`

- [ ] **Step 1: Add the shared fixtures to `tests/conftest.py`**

```python
from tests.support.auth import (  # noqa: E402
    ACME_JWKS, INITECH_JWKS, NOW, FakeJwksFetcher, MutableClock, acme_jwks, claims, generate_test_keys, mint,
    public_jwk,
)

_KEYS = generate_test_keys()


@pytest.fixture(scope="session")
def keys():
    return _KEYS


@pytest.fixture
def registry():
    from hrms_plugin.security.tenant_registry import load_registry

    return load_registry(Path(__file__).parent / "fixtures" / "tenant_registry.yaml")


@pytest.fixture
def jwks_fetcher(keys):
    return FakeJwksFetcher({ACME_JWKS: acme_jwks(keys), INITECH_JWKS: {"keys": [public_jwk(keys.initech_rsa, "initech-rsa")]}})


@pytest.fixture
def clock():
    return MutableClock(NOW)


@pytest.fixture
def verifier(registry, jwks_fetcher, clock):
    from hrms_plugin.security.jwks_cache import JwksCache
    from hrms_plugin.security.token_verifier import TokenVerifier

    return TokenVerifier(registry, JwksCache(jwks_fetcher, clock=MutableClock(0.0)), clock=clock)


@pytest.fixture
def make_token(keys):
    def _make(**kw):
        return mint(keys.rsa, "acme-rsa", claims(**kw))

    return _make


@pytest.fixture
def bearer(make_token):
    def _headers(**kw):
        return {"Authorization": f"Bearer {make_token(**kw)}"}

    return _headers
```

- [ ] **Step 2: Write the failing tests**

`tests/unit/test_token_verifier.py`:
```python
"""Token verification: header, issuer/tenant resolution, signature, time windows, claims (spec §6.2)."""

import logging

import pytest
from cryptography.hazmat.primitives import serialization

from hrms_plugin.security.rbac import UserRole
from hrms_plugin.security.token_verifier import MAX_TOKEN_BYTES, AuthError
from tests.support.auth import INITECH_ISSUER, NOW, claims, forge, forge_hs256, mint


async def ok(verifier, token):
    return await verifier.verify([f"Bearer {token}"])


async def reason(verifier, headers):
    with pytest.raises(AuthError) as info:
        await verifier.verify(headers)
    return info.value.reason


# Positive ---------------------------------------------------------------

async def test_valid_rs256(verifier, make_token):
    auth = await ok(verifier, make_token(roles=["hr_manager"]))
    assert (auth.user_id, auth.tenant_id, auth.roles) == ("user-1", "acme", frozenset({UserRole.HR_ADMIN}))
    assert auth.issued_at == NOW and auth.token_id == "jti-user-1"


async def test_valid_es256(verifier, keys):
    auth = await ok(verifier, mint(keys.ec, "acme-ec", claims(), alg="ES256"))
    assert auth.tenant_id == "acme"


async def test_lowercase_scheme_accepted(verifier, make_token):
    assert (await verifier.verify([f"bearer {make_token()}"])).tenant_id == "acme"


async def test_shared_issuer_resolves_each_tenant(verifier, make_token):
    assert (await ok(verifier, make_token(tenant="globex"))).tenant_id == "globex"
    assert (await ok(verifier, make_token(tenant="acme"))).tenant_id == "acme"


async def test_custom_claim_names(verifier, keys):
    token = mint(keys.initech_rsa, "initech-rsa", claims(issuer=INITECH_ISSUER, tenant="initech", roles=["hr"],
                                                          tenant_claim="org", roles_claim="groups", lifetime=60))
    assert (await ok(verifier, token)).roles == frozenset({UserRole.HR_ADMIN})


async def test_mixed_roles_union_and_unmapped_logged(verifier, make_token, caplog):
    caplog.set_level(logging.INFO, logger="hrms_plugin.security.auth")
    auth = await ok(verifier, make_token(roles=["hr_manager", "staff", "janitor"]))
    assert auth.roles == frozenset({UserRole.HR_ADMIN, UserRole.EMPLOYEE})
    assert any(getattr(r, "reason", "") == "UNMAPPED_ROLES" and "janitor" in r.roles_unmapped for r in caplog.records)


async def test_only_unmapped_roles_gives_empty_role_set(verifier, make_token):
    assert (await ok(verifier, make_token(roles=["janitor"]))).roles == frozenset()


async def test_audience_list_containing_ours_accepted(verifier, make_token):
    assert (await ok(verifier, make_token(audience=["other", "hrms-agentic-plugin"]))).tenant_id == "acme"


# Header -----------------------------------------------------------------

@pytest.mark.parametrize("headers,expected", [
    ([], "HEADER_COUNT"),
    (["Bearer a", "Bearer b"], "HEADER_COUNT"),
    (["Basic dXNlcjpwYXNz"], "SCHEME"),
    (["Bearer"], "SCHEME"),
    (["Bearer    "], "EMPTY_TOKEN"),
    (["Bearer not.a.jwt"], "MALFORMED"),
])
async def test_header_rejections(verifier, headers, expected):
    assert await reason(verifier, headers) == expected


async def test_token_size_boundary(verifier):
    assert await reason(verifier, ["Bearer " + "a" * MAX_TOKEN_BYTES]) != "TOKEN_TOO_LARGE"
    assert await reason(verifier, ["Bearer " + "a" * (MAX_TOKEN_BYTES + 1)]) == "TOKEN_TOO_LARGE"


# Algorithm and key ----------------------------------------------------------

async def test_alg_none_rejected(verifier):
    assert await reason(verifier, [f"Bearer {forge({'alg': 'none', 'kid': 'acme-rsa'}, claims())}"]) == "ALG_NOT_ALLOWED"


async def test_hs256_with_public_key_as_secret_rejected(verifier, keys):
    pem = keys.rsa.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    assert await reason(verifier, [f"Bearer {forge_hs256(claims(), pem, 'acme-rsa')}"]) == "ALG_NOT_ALLOWED"


async def test_es256_rejected_when_tenant_allows_only_rs256(verifier, keys):
    token = mint(keys.ec, "acme-ec", claims(tenant="globex"), alg="ES256")
    assert await reason(verifier, [f"Bearer {token}"]) == "ALG_NOT_ALLOWED"


async def test_missing_kid_rejected(verifier, keys):
    import jwt as pyjwt

    token = pyjwt.encode(claims(), keys.rsa, algorithm="RS256")
    assert await reason(verifier, [f"Bearer {token}"]) == "KID_MISSING"


async def test_path_like_kid_rejected(verifier, keys):
    assert await reason(verifier, [f"Bearer {mint(keys.rsa, '../x', claims())}"]) == "UNKNOWN_KID"


async def test_jku_header_is_ignored(verifier, keys, jwks_fetcher):
    token = mint(keys.rsa, "acme-rsa", claims(), headers={"jku": "https://attacker.test/jwks.json"})
    assert (await ok(verifier, token)).tenant_id == "acme"
    assert "https://attacker.test/jwks.json" not in jwks_fetcher.calls


async def test_bad_signature_rejected(verifier, keys):
    token = mint(keys.initech_rsa, "acme-rsa", claims())  # signed with a different key under acme's kid
    assert await reason(verifier, [f"Bearer {token}"]) == "BAD_SIGNATURE"


# Issuer / tenant / audience -------------------------------------------------------

@pytest.mark.parametrize("kw,expected", [
    ({"issuer": "https://nope.test"}, "UNKNOWN_ISSUER"),
    ({"tenant": "initech"}, "UNKNOWN_TENANT"),      # initech exists, but under another issuer
    ({"tenant": "ACME"}, "UNKNOWN_TENANT"),
    ({"tenant": "unregistered"}, "UNKNOWN_TENANT"),
    ({"audience": "someone-else"}, "BAD_AUDIENCE"),
])
async def test_issuer_tenant_audience_rejections(verifier, make_token, kw, expected):
    assert await reason(verifier, [f"Bearer {make_token(**kw)}"]) == expected


async def test_missing_iss_rejected(verifier, make_token, keys):
    payload = claims()
    del payload["iss"]
    assert await reason(verifier, [f"Bearer {mint(keys.rsa, 'acme-rsa', payload)}"]) == "UNKNOWN_ISSUER"


# Time windows (acme leeway 30 s, max lifetime 3600 s) --------------------------------

@pytest.mark.parametrize("missing", ["exp", "iat"])
async def test_missing_time_claims_rejected(verifier, keys, missing):
    payload = claims()
    del payload[missing]
    assert await reason(verifier, [f"Bearer {mint(keys.rsa, 'acme-rsa', payload)}"]) == "MISSING_CLAIM"


async def test_exp_boundary(verifier, make_token):
    assert await ok(verifier, make_token(iat=NOW - 630, lifetime=600))           # exp = now - 30
    assert await reason(verifier, [f"Bearer {make_token(iat=NOW - 631, lifetime=600)}"]) == "EXPIRED"


async def test_nbf_boundary(verifier, make_token):
    assert await ok(verifier, make_token(nbf=NOW + 30))
    assert await reason(verifier, [f"Bearer {make_token(nbf=NOW + 31)}"]) == "NOT_YET_VALID"


async def test_iat_boundary(verifier, make_token):
    assert await ok(verifier, make_token(iat=NOW + 30))
    assert await reason(verifier, [f"Bearer {make_token(iat=NOW + 31)}"]) == "IAT_IN_FUTURE"


async def test_lifetime_boundary(verifier, make_token):
    assert await ok(verifier, make_token(lifetime=3600))
    assert await reason(verifier, [f"Bearer {make_token(lifetime=3601)}"]) == "LIFETIME_TOO_LONG"


@pytest.mark.parametrize("field,value", [("exp", "soon"), ("iat", True), ("nbf", "later")])
async def test_non_numeric_time_claims_rejected(verifier, keys, field, value):
    payload = claims()
    payload[field] = value
    assert await reason(verifier, [f"Bearer {mint(keys.rsa, 'acme-rsa', payload)}"]) in {
        "TIME_CLAIM_INVALID", "MALFORMED", "INVALID_TOKEN"}


# Identity claims ----------------------------------------------------------------

@pytest.mark.parametrize("sub", ["", "x" * 257, 42])
async def test_invalid_subject_rejected(verifier, make_token, sub):
    assert await reason(verifier, [f"Bearer {make_token(sub=sub)}"]) == "SUBJECT_INVALID"


async def test_subject_boundary_256_accepted(verifier, make_token):
    assert (await ok(verifier, make_token(sub="x" * 256))).user_id == "x" * 256


async def test_missing_subject_rejected(verifier, keys):
    payload = claims()
    del payload["sub"]
    assert await reason(verifier, [f"Bearer {mint(keys.rsa, 'acme-rsa', payload)}"]) == "SUBJECT_INVALID"


@pytest.mark.parametrize("roles", ["hr_manager", ["hr_manager", 7], None])
async def test_invalid_roles_shape_rejected(verifier, keys, roles):
    payload = claims()
    payload["roles"] = roles
    assert await reason(verifier, [f"Bearer {mint(keys.rsa, 'acme-rsa', payload)}"]) == "ROLES_INVALID"


async def test_missing_roles_claim_rejected(verifier, keys):
    payload = claims()
    del payload["roles"]
    assert await reason(verifier, [f"Bearer {mint(keys.rsa, 'acme-rsa', payload)}"]) == "ROLES_INVALID"


async def test_roles_count_boundary(verifier, make_token):
    assert await ok(verifier, make_token(roles=["staff"] * 50))
    assert await reason(verifier, [f"Bearer {make_token(roles=['staff'] * 51)}"]) == "ROLES_INVALID"
```

- [ ] **Step 3: Run** → `ModuleNotFoundError: hrms_plugin.security.token_verifier`.

- [ ] **Step 4: Implement**

`hrms_plugin/security/token_verifier.py`:
```python
"""Verifies host-signed JWTs against the tenant registry and produces an AuthContext."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, FrozenSet, Optional, Sequence

import jwt

from hrms_plugin.security.jwks_cache import JwksCache, UnknownSigningKey
from hrms_plugin.security.rbac import AuthContext, UserRole
from hrms_plugin.security.tenant_registry import TenantConfig, TenantRegistry

logger = logging.getLogger("hrms_plugin.security.auth")

MAX_TOKEN_BYTES = 8192
MAX_ROLES = 50
MAX_SUBJECT_LENGTH = 256

_JWT_ERROR_REASONS = (
    (jwt.InvalidSignatureError, "BAD_SIGNATURE"),
    (jwt.InvalidAudienceError, "BAD_AUDIENCE"),
    (jwt.InvalidIssuerError, "BAD_ISSUER"),
    (jwt.MissingRequiredClaimError, "MISSING_CLAIM"),
    (jwt.InvalidAlgorithmError, "ALG_NOT_ALLOWED"),
    (jwt.DecodeError, "MALFORMED"),
)


class AuthError(Exception):
    """Token rejected. `reason` is logged; it is never returned to the caller."""

    def __init__(self, reason: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(reason)
        self.reason = reason
        self.details = dict(details or {})


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class TokenVerifier:
    def __init__(self, registry: TenantRegistry, jwks_cache: JwksCache, clock: Callable[[], float] = time.time):
        self._registry = registry
        self._jwks = jwks_cache
        self._clock = clock

    async def verify(self, authorization_headers: Sequence[str]) -> AuthContext:
        token = self._extract_token(authorization_headers)
        try:
            header = jwt.get_unverified_header(token)
            unverified = jwt.decode(token, options={"verify_signature": False})
        except jwt.PyJWTError:
            raise AuthError("MALFORMED") from None

        details: Dict[str, Any] = {}
        issuer = unverified.get("iss")
        claim_names = self._registry.claims_for_issuer(issuer) if isinstance(issuer, str) else None
        if claim_names is None:
            raise AuthError("UNKNOWN_ISSUER")
        details["iss"] = issuer

        tenant_claim = unverified.get(claim_names.tenant)
        tenant = self._registry.find(issuer, tenant_claim) if isinstance(tenant_claim, str) else None
        if tenant is None:
            raise AuthError("UNKNOWN_TENANT", details)
        details["tenant"] = tenant.tenant_id

        alg = header.get("alg")
        if alg not in tenant.algorithms:
            raise AuthError("ALG_NOT_ALLOWED", details)
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise AuthError("KID_MISSING", details)
        try:
            key = await self._jwks.get_signing_key(tenant.jwks_url, kid)
        except UnknownSigningKey:
            raise AuthError("UNKNOWN_KID", details) from None

        try:
            payload = jwt.decode(
                token,
                key=key,
                algorithms=[alg],
                audience=tenant.audience,
                issuer=tenant.issuer,
                options={"verify_exp": False, "verify_nbf": False, "verify_iat": False, "require": ["exp", "iat"]},
            )
        except jwt.PyJWTError as exc:
            raise AuthError(self._reason_for(exc), details) from None

        details["sub"] = payload.get(claim_names.subject)
        details["jti"] = payload.get("jti")
        self._check_times(payload, tenant, details)

        subject = payload.get(claim_names.subject)
        if not isinstance(subject, str) or not subject or len(subject) > MAX_SUBJECT_LENGTH:
            raise AuthError("SUBJECT_INVALID", details)
        if payload.get(claim_names.tenant) != tenant.tenant_id:
            raise AuthError("TENANT_CLAIM_MISMATCH", details)
        roles = self._map_roles(payload.get(claim_names.roles), tenant, details)
        jti = payload.get("jti")
        return AuthContext(
            user_id=subject,
            tenant_id=tenant.tenant_id,
            roles=roles,
            issued_at=float(payload["iat"]),
            token_id=jti if isinstance(jti, str) else None,
        )

    @staticmethod
    def _extract_token(headers: Sequence[str]) -> str:
        if len(headers) != 1:
            raise AuthError("HEADER_COUNT")
        scheme, _, token = headers[0].strip().partition(" ")
        if scheme.lower() != "bearer" or not _:
            raise AuthError("SCHEME")
        token = token.strip()
        if not token:
            raise AuthError("EMPTY_TOKEN")
        if len(token.encode("utf-8")) > MAX_TOKEN_BYTES:
            raise AuthError("TOKEN_TOO_LARGE")
        return token

    @staticmethod
    def _reason_for(exc: jwt.PyJWTError) -> str:
        for exc_type, reason in _JWT_ERROR_REASONS:
            if isinstance(exc, exc_type):
                return reason
        return "INVALID_TOKEN"

    def _check_times(self, payload: Dict[str, Any], tenant: TenantConfig, details: Dict[str, Any]) -> None:
        now = self._clock()
        leeway = tenant.leeway_seconds
        exp, iat = payload["exp"], payload["iat"]
        if not _is_number(exp) or not _is_number(iat):
            raise AuthError("TIME_CLAIM_INVALID", details)
        if now > exp + leeway:
            raise AuthError("EXPIRED", details)
        if iat > now + leeway:
            raise AuthError("IAT_IN_FUTURE", details)
        if "nbf" in payload:
            nbf = payload["nbf"]
            if not _is_number(nbf):
                raise AuthError("TIME_CLAIM_INVALID", details)
            if nbf > now + leeway:
                raise AuthError("NOT_YET_VALID", details)
        if exp - iat > tenant.max_token_lifetime_seconds:
            raise AuthError("LIFETIME_TOO_LONG", details)

    @staticmethod
    def _map_roles(raw: Any, tenant: TenantConfig, details: Dict[str, Any]) -> FrozenSet[UserRole]:
        if not isinstance(raw, list) or len(raw) > MAX_ROLES or not all(isinstance(r, str) for r in raw):
            raise AuthError("ROLES_INVALID", details)
        mapped = {tenant.role_map[r] for r in raw if r in tenant.role_map}
        unmapped = sorted({r for r in raw if r not in tenant.role_map})
        if unmapped:
            logger.info(
                "unmapped host roles ignored",
                extra={"reason": "UNMAPPED_ROLES", "tenant": tenant.tenant_id, "roles_unmapped": unmapped},
            )
        return frozenset(mapped)
```

`_extract_token` rejects `"Bearer"` with no space (`_` empty) as `SCHEME`, matching the header test table.

- [ ] **Step 5: Verify** — the verifier tests pass. If PyJWT raises a different exception class than the table expects for a case, **stop and report the actual class**; do not loosen the expected reason. The one exception is `test_non_numeric_time_claims_rejected`, which already accepts any of three reasons because PyJWT validates some claim types itself.

- [ ] **Step 6: Commit** — `feat(security): token verifier for host-signed RS256/ES256 tokens`

---

### Task 11: HTTP auth dependencies (spec §6.6 header, §6.7, §6.9)

**Files:**
- Create: `hrms_plugin/security/http_auth.py`
- Test: `tests/unit/test_http_auth.py`

- [ ] **Step 1: Write the failing tests** (a minimal FastAPI app isolates the dependencies)

```python
"""authenticate/require: status codes, bodies, headers, freshness, logging (spec §6.6, §6.7, §6.9)."""

import logging

import pytest
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from hrms_plugin.security.http_auth import ApiError, require
from hrms_plugin.security.jwks_cache import JwksCache
from hrms_plugin.security.rbac import AuthContext, Permission
from tests.support.auth import ACME_JWKS, NOW, MutableClock


@pytest.fixture
def client(verifier, clock):
    app = FastAPI()
    app.state.token_verifier = verifier
    app.state.clock = clock

    @app.exception_handler(ApiError)
    async def _h(_request, exc: ApiError):
        return JSONResponse(status_code=exc.status_code, content=exc.body, headers=exc.headers)

    @app.get("/copilot")
    def copilot(auth: AuthContext = Depends(require(Permission.USE_COPILOT))):
        return {"sub": auth.user_id, "tenant": auth.tenant_id}

    @app.post("/remediate")
    def remediate(auth: AuthContext = Depends(require(Permission.EXECUTE_REMEDIATION, max_token_age_seconds=300))):
        return {"ok": True}

    return TestClient(app)


def test_valid_token_passes(client, bearer):
    assert client.get("/copilot", headers=bearer(roles=["staff"])).json() == {"sub": "user-1", "tenant": "acme"}


def test_no_token_401_with_challenge(client):
    res = client.get("/copilot")
    assert res.status_code == 401
    assert res.json() == {"error": "UNAUTHENTICATED"}
    assert res.headers["www-authenticate"] == 'Bearer error="invalid_token"'


def test_duplicate_authorization_headers_401(client, make_token):
    t = make_token()
    res = client.get("/copilot", headers=[("Authorization", f"Bearer {t}"), ("Authorization", f"Bearer {t}")])
    assert res.status_code == 401


def test_missing_permission_403(client, bearer):
    res = client.post("/remediate", headers=bearer(roles=["staff"]))
    assert res.status_code == 403
    assert res.json() == {"error": "FORBIDDEN", "permission": "EXECUTE_REMEDIATION"}


def test_no_mapped_roles_403(client, bearer):
    assert client.get("/copilot", headers=bearer(roles=["janitor"])).status_code == 403


def test_tenant_header_equal_ok_absent_ok_different_403(client, bearer):
    h = bearer()
    assert client.get("/copilot", headers=h).status_code == 200
    assert client.get("/copilot", headers={**h, "X-Tenant-Id": "acme"}).status_code == 200
    res = client.get("/copilot", headers={**h, "X-Tenant-Id": "globex"})
    assert res.status_code == 403 and res.json() == {"error": "TENANT_MISMATCH"}


def test_freshness_boundary(client, bearer):
    assert client.post("/remediate", headers=bearer(iat=NOW - 300)).status_code == 200
    res = client.post("/remediate", headers=bearer(iat=NOW - 301))
    assert res.status_code == 401 and res.json() == {"error": "REAUTH_REQUIRED"}


def test_freshness_applies_only_where_declared(client, bearer):
    assert client.get("/copilot", headers=bearer(iat=NOW - 301)).status_code == 200


def test_forbidden_checked_before_freshness(client, bearer):
    assert client.post("/remediate", headers=bearer(roles=["staff"], iat=NOW - 301)).status_code == 403


def test_body_identical_for_different_failures_but_reasons_logged(client, bearer, keys, caplog):
    from tests.support.auth import claims, mint

    caplog.set_level(logging.WARNING, logger="hrms_plugin.security.auth")
    expired = client.get("/copilot", headers=bearer(iat=NOW - 1000))
    bad_sig = client.get("/copilot", headers={"Authorization": f"Bearer {mint(keys.initech_rsa, 'acme-rsa', claims())}"})
    assert expired.json() == bad_sig.json() == {"error": "UNAUTHENTICATED"}
    reasons = {getattr(r, "reason", None) for r in caplog.records}
    assert {"EXPIRED", "BAD_SIGNATURE"} <= reasons


def test_token_never_logged(client, make_token, caplog):
    caplog.set_level(logging.DEBUG)
    token = make_token(iat=NOW - 1000)
    client.get("/copilot", headers={"Authorization": f"Bearer {token}"})
    assert token not in caplog.text
    assert all(token not in str(getattr(r, "__dict__", {})) for r in caplog.records)


def test_jwks_outage_is_503(registry, jwks_fetcher, clock, make_token):
    from hrms_plugin.security.token_verifier import TokenVerifier

    jwks_fetcher.failures.add(ACME_JWKS)
    app = FastAPI()
    app.state.token_verifier = TokenVerifier(registry, JwksCache(jwks_fetcher, clock=MutableClock(0.0)), clock=clock)
    app.state.clock = clock

    @app.exception_handler(ApiError)
    async def _h(_request, exc):
        return JSONResponse(status_code=exc.status_code, content=exc.body, headers=exc.headers)

    @app.get("/x")
    def x(auth=Depends(require(Permission.USE_COPILOT))):
        return {}

    res = TestClient(app).get("/x", headers={"Authorization": f"Bearer {make_token()}"})
    assert res.status_code == 503 and res.json() == {"error": "AUTH_UNAVAILABLE"}


def test_require_exposes_permission_for_route_audits():
    dep = require(Permission.VIEW_COMPLIANCE, max_token_age_seconds=300)
    assert dep.permission is Permission.VIEW_COMPLIANCE and dep.max_token_age_seconds == 300
```

- [ ] **Step 2: Run** → `ModuleNotFoundError: hrms_plugin.security.http_auth`.

- [ ] **Step 3: Implement**

`hrms_plugin/security/http_auth.py`:
```python
"""FastAPI dependencies that authenticate the caller and enforce a permission per route."""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from fastapi import Request

from hrms_plugin.security.jwks_cache import AuthUnavailable
from hrms_plugin.security.rbac import AuthContext, Permission, RbacPolicyEnforcer
from hrms_plugin.security.token_verifier import AuthError

logger = logging.getLogger("hrms_plugin.security.auth")

_CHALLENGE = {"WWW-Authenticate": 'Bearer error="invalid_token"'}
_enforcer = RbacPolicyEnforcer()


class ApiError(Exception):
    """An error rendered verbatim as the JSON response body."""

    def __init__(self, status_code: int, body: Dict[str, str], headers: Optional[Dict[str, str]] = None):
        super().__init__(body.get("error", ""))
        self.status_code = status_code
        self.body = body
        self.headers = headers


def _log_fields(auth: AuthContext, reason: str) -> Dict[str, Any]:
    return {"reason": reason, "tenant": auth.tenant_id, "sub": auth.user_id, "jti": auth.token_id}


async def authenticate(request: Request) -> AuthContext:
    verifier = request.app.state.token_verifier
    try:
        auth = await verifier.verify(request.headers.getlist("authorization"))
    except AuthError as exc:
        logger.warning("authentication rejected", extra={"reason": exc.reason, **exc.details})
        raise ApiError(401, {"error": "UNAUTHENTICATED"}, _CHALLENGE) from None
    except AuthUnavailable as exc:
        logger.error("authentication unavailable", extra={"reason": "AUTH_UNAVAILABLE", "jwks_url": exc.jwks_url})
        raise ApiError(503, {"error": "AUTH_UNAVAILABLE"}) from None

    header_tenant = request.headers.get("x-tenant-id")
    if header_tenant is not None and header_tenant != auth.tenant_id:
        logger.warning("tenant header mismatch", extra=_log_fields(auth, "TENANT_MISMATCH"))
        raise ApiError(403, {"error": "TENANT_MISMATCH"})
    return auth


def require(permission: Permission, *, max_token_age_seconds: Optional[int] = None) -> Callable[..., Any]:
    """Dependency factory: authenticate, check the permission, then (optionally) token freshness."""

    async def dependency(request: Request) -> AuthContext:
        auth = await authenticate(request)
        if not _enforcer.is_authorized(auth, permission):
            logger.warning("permission denied", extra={**_log_fields(auth, "FORBIDDEN"), "permission": permission.value})
            raise ApiError(403, {"error": "FORBIDDEN", "permission": permission.value})
        if max_token_age_seconds is not None:
            age = request.app.state.clock() - (auth.issued_at or 0.0)
            if age > max_token_age_seconds:
                logger.warning("token too old for this action", extra=_log_fields(auth, "REAUTH_REQUIRED"))
                raise ApiError(401, {"error": "REAUTH_REQUIRED"}, _CHALLENGE)
        return auth

    dependency.permission = permission  # type: ignore[attr-defined]
    dependency.max_token_age_seconds = max_token_age_seconds  # type: ignore[attr-defined]
    return dependency
```

- [ ] **Step 4: Verify** — `test_http_auth.py` passes; the full suite shows only the known failures.

- [ ] **Step 5: Commit** — `feat(security): authenticate/require dependencies with uniform error bodies`

---

### Task 12: App factory, per-route permissions, request models, CORS, docs flag (spec §6.4, §6.6, §6.8, A5-API, AM1, AM2, AM5)

**Files:**
- Modify: `hrms_plugin/server/app.py` (restructure around `router` + `create_app`)
- Modify: `tests/conftest.py` (`make_client` fixture)
- Rewrite: `tests/unit/test_server_routes.py`, `tests/e2e/test_copilot_e2e.py`
- Modify: `tests/unit/test_agentic_llm.py` (client fixture and headers only)
- Test: `tests/unit/test_route_security.py`

- [ ] **Step 1: Add the `make_client` fixture to `tests/conftest.py`**

```python
@pytest.fixture
def make_client(registry, jwks_fetcher, clock):
    from fastapi.testclient import TestClient

    from hrms_plugin.security.jwks_cache import JwksCache
    from hrms_plugin.server.app import create_app

    def _make(supervisor_agent=None, env=None):
        app = create_app(registry=registry, jwks_cache=JwksCache(jwks_fetcher, clock=MutableClock(0.0)),
                         clock=clock, supervisor_agent=supervisor_agent, env=env or {})
        return TestClient(app)

    return _make
```

- [ ] **Step 2: Write the failing route-security tests**

`tests/unit/test_route_security.py`:
```python
"""Every route except /healthz requires its declared permission (spec §6.4, §6.6, §6.8, AM2, AM5)."""

import pytest
from fastapi.routing import APIRoute

from hrms_plugin.security.rbac import Permission as P
from hrms_plugin.server.app import create_app

EXPECTED = {
    ("POST", "/v1/chat"): P.USE_COPILOT,
    ("POST", "/v1/chat/stream"): P.USE_COPILOT,
    ("POST", "/api/compliance/agent/chat"): P.USE_COPILOT,
    ("POST", "/v1/voice/transcribe"): P.USE_COPILOT,
    ("POST", "/v1/voice/synthesize"): P.USE_COPILOT,
    ("POST", "/v1/voice/turn"): P.USE_COPILOT,
    ("POST", "/v1/schema/introspect"): P.MANAGE_SCHEMA,
    ("POST", "/v1/schema/synthesize"): P.MANAGE_SCHEMA,
    ("POST", "/v1/schema/synthesize/agentic"): P.MANAGE_SCHEMA,
    ("GET", "/v1/schema/mappings"): P.MANAGE_SCHEMA,
    ("GET", "/v1/schema/mappings/{vendor}/{entity_type}"): P.MANAGE_SCHEMA,
    ("POST", "/v1/remediate/execute"): P.EXECUTE_REMEDIATION,
    ("GET", "/v1/compliance/diagnose"): P.VIEW_COMPLIANCE,
    ("GET", "/api/compliance/agent/diagnose"): P.VIEW_COMPLIANCE,
    ("GET", "/v1/compliance/dashboard/live"): P.VIEW_COMPLIANCE,
    ("GET", "/api/compliance/dashboard/live"): P.VIEW_COMPLIANCE,
    ("POST", "/api/requisitions/generate-archetypes"): P.MANAGE_RECRUITMENT,
    ("POST", "/v1/requisitions/generate-archetypes"): P.MANAGE_RECRUITMENT,
}
CONCRETE = {"/v1/schema/mappings/{vendor}/{entity_type}": "/v1/schema/mappings/zoho/EMPLOYEE"}
# Role in the fixture registry that LACKS each permission (to prove 403).
LACKING = {P.USE_COPILOT: "janitor", P.MANAGE_SCHEMA: "hr_manager", P.EXECUTE_REMEDIATION: "staff",
           P.VIEW_COMPLIANCE: "staff", P.MANAGE_RECRUITMENT: "staff"}
HOLDING = {P.USE_COPILOT: "staff", P.MANAGE_SCHEMA: "platform_admin", P.EXECUTE_REMEDIATION: "hr_manager",
           P.VIEW_COMPLIANCE: "hr_manager", P.MANAGE_RECRUITMENT: "recruiter"}


def _routes(app):
    out = {}
    for route in app.routes:
        if isinstance(route, APIRoute):
            perms = [getattr(d.call, "permission", None) for d in route.dependant.dependencies]
            for method in route.methods:
                out[(method, route.path)] = [p for p in perms if p is not None]
    return out


def test_route_table_matches_exactly(registry):
    from hrms_plugin.security.jwks_cache import JwksCache

    routes = _routes(create_app(registry=registry, jwks_cache=JwksCache(lambda u: None), env={}))
    public = {k for k, v in routes.items() if not v}
    assert public == {("GET", "/healthz")}
    assert {k: v[0] for k, v in routes.items() if v} == EXPECTED
    assert all(len(v) <= 1 for v in routes.values())


def test_remediation_route_requires_fresh_token(registry):
    from hrms_plugin.security.jwks_cache import JwksCache

    app = create_app(registry=registry, jwks_cache=JwksCache(lambda u: None), env={})
    route = next(r for r in app.routes if isinstance(r, APIRoute) and r.path == "/v1/remediate/execute")
    assert [d.call.max_token_age_seconds for d in route.dependant.dependencies if hasattr(d.call, "permission")] == [300]


def _call(client, method, path, headers):
    path = CONCRETE.get(path, path)
    return client.request(method, path, headers=headers, json={} if method == "POST" else None)


@pytest.mark.parametrize("method,path", sorted(EXPECTED))
def test_no_token_401(make_client, method, path):
    assert _call(make_client(), method, path, {}).status_code == 401


@pytest.mark.parametrize("method,path", sorted(EXPECTED))
def test_lacking_permission_403(make_client, bearer, method, path):
    res = _call(make_client(), method, path, bearer(roles=[LACKING[EXPECTED[(method, path)]]]))
    assert res.status_code == 403


@pytest.mark.parametrize("method,path", sorted(EXPECTED))
def test_holding_permission_passes_auth(make_client, bearer, method, path):
    # Body {} may be invalid for the route (422) — the point is auth no longer blocks it.
    res = _call(make_client(), method, path, bearer(roles=[HOLDING[EXPECTED[(method, path)]]]))
    assert res.status_code not in (401, 403)


def test_two_roles_one_granting_is_allowed(make_client, bearer):
    assert make_client().get("/v1/compliance/dashboard/live", headers=bearer(roles=["staff", "hr_manager"])).status_code == 200


def test_healthz_public(make_client):
    assert make_client().get("/healthz").status_code == 200


# AM2 — docs ------------------------------------------------------------------

@pytest.mark.parametrize("value", [None, "True", "1", "yes", ""])
def test_docs_disabled_unless_exactly_true(make_client, value):
    client = make_client(env={} if value is None else {"HRMS_ENABLE_API_DOCS": value})
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_docs_enabled_with_true(make_client):
    client = make_client(env={"HRMS_ENABLE_API_DOCS": "true"})
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 200


# §6.8 — CORS ----------------------------------------------------------------

def _preflight(client, origin):
    return client.options("/v1/chat", headers={"Origin": origin, "Access-Control-Request-Method": "POST",
                                                "Access-Control-Request-Headers": "authorization,content-type"})


def test_cors_allowed_origin(make_client):
    res = _preflight(make_client(), "https://hr.acme.test")
    assert res.headers.get("access-control-allow-origin") == "https://hr.acme.test"
    assert res.headers.get("access-control-allow-credentials") != "true"


@pytest.mark.parametrize("origin", ["https://evil.test", "http://hr.acme.test", "https://hr.acme.test:444",
                                    "https://hr.initech.test"])
def test_cors_disallowed_origins(make_client, origin):
    assert "access-control-allow-origin" not in _preflight(make_client(), origin).headers


def test_cors_port_origin_allowed(make_client):
    res = _preflight(make_client(), "https://hr.initech.test:8443")
    assert res.headers.get("access-control-allow-origin") == "https://hr.initech.test:8443"


# A5 / AM5 — strict jurisdiction ---------------------------------------------------

@pytest.mark.parametrize("value", ["ae", "UAE", "", "XX", "GLOBAL"])
def test_chat_rejects_invalid_jurisdiction(make_client, bearer, value):
    res = make_client().post("/v1/chat", headers=bearer(roles=["staff"]), json={"message": "hi", "jurisdiction": value})
    assert res.status_code == 422


@pytest.mark.parametrize("value", ["ae", "UAE", "", "XX", "GLOBAL"])
def test_voice_turn_rejects_invalid_jurisdiction(make_client, bearer, value):
    res = make_client().post("/v1/voice/turn", headers=bearer(roles=["staff"]),
                             json={"audio_base64": "", "jurisdiction": value})
    assert res.status_code == 422


# §6.6 — remediation body and approver identity ----------------------------------------

REMEDIATION = {"violation_id": "V1", "target_entity": "EMPLOYEE", "entity_id": "EMP-1", "patch_payload": {"a": 1}}


@pytest.mark.parametrize("extra", [{"approver_role": "HR_ADMIN"}, {"approver_id": "MGR"}, {"anything": 1}])
def test_remediation_rejects_unknown_fields(make_client, bearer, extra):
    res = make_client().post("/v1/remediate/execute", headers=bearer(), json={**REMEDIATION, **extra})
    assert res.status_code == 422


def test_remediation_approver_is_token_subject(make_client, bearer):
    res = make_client().post("/v1/remediate/execute", headers=bearer(sub="mgr-42"), json=REMEDIATION)
    assert res.status_code == 200
    trace = res.json()["audit_tracing"]
    assert trace["X-HITL-Approver-Id"] == "mgr-42" and trace["X-Tenant-Id"] == "acme"


def test_handlers_use_token_tenant(make_client, bearer):
    res = make_client().get("/v1/schema/mappings", headers={**bearer(roles=["platform_admin"]), "X-Tenant-Id": "acme"})
    assert res.json()["tenant_id"] == "acme"


def test_stream_without_token_is_plain_401(make_client):
    res = make_client().post("/v1/chat/stream", json={"message": "hi"})
    assert res.status_code == 401
    assert not res.headers.get("content-type", "").startswith("text/event-stream")


# AM4 — in-process hostile smoke with the real verifier ----------------------------------------

def test_employee_cannot_execute_remediation(make_client, bearer):
    res = make_client().post("/v1/remediate/execute", headers=bearer(roles=["staff"]), json=REMEDIATION)
    assert res.status_code == 403 and res.json()["permission"] == "EXECUTE_REMEDIATION"
```

- [ ] **Step 3: Run** → `ImportError: cannot import name 'create_app'`.

- [ ] **Step 4: Implement `app.py`**

Edit `hrms_plugin/server/app.py` as follows. Unchanged blocks are named explicitly; copy them as they are.

1. **Imports** — replace the import block (lines 5–35) with:
```python
import base64
import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Mapping, Optional

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict

from hrms_plugin.agents.compliance import ComplianceAgent
from hrms_plugin.agents.leave_attendance import LeaveAttendanceAgent
from hrms_plugin.agents.recruitment import RecruitmentAgent
from hrms_plugin.agents.statutory_payroll import StatutoryPayrollAgent
from hrms_plugin.agents.supervisor import AgentResponse, SupervisorAgent
from hrms_plugin.agents.voice import VoiceAgent
from hrms_plugin.rag.store import StatutoryKnowledgeBase
from hrms_plugin.schema.canonical import EntityType
from hrms_plugin.schema.introspector import SchemaIntrospector
from hrms_plugin.schema.synthesizer import MappingSynthesizer
from hrms_plugin.security.audit import HostAuditGateway
from hrms_plugin.security.http_auth import ApiError, require
from hrms_plugin.security.jwks_cache import JwksCache, make_httpx_fetcher
from hrms_plugin.security.masking import PiiMaskingGateway
from hrms_plugin.security.rbac import AuthContext, Permission, PermissionDenied
from hrms_plugin.security.tenant_registry import TenantRegistry, load_registry_from_env
from hrms_plugin.security.token_verifier import TokenVerifier
from hrms_plugin.storage.mapping_store import MappingStore
from hrms_plugin.storage.sqlite_store import SqliteKeyValueStore
from hrms_plugin.storage.vault_store import PiiVaultStore

API_DOCS_ENV_VAR = "HRMS_ENABLE_API_DOCS"
REMEDIATION_MAX_TOKEN_AGE_SECONDS = 300
JurisdictionCode = Literal["IN", "AE", "SA", "US"]
```

2. **Delete** the module-level `app = FastAPI(...)` and `app.add_middleware(...)` blocks (old lines 37–49).

3. **Singletons** — keep lines 52–76 unchanged except delete `rbac_enforcer = RbacPolicyEnforcer()`. The `_storage` line was already changed in Task 3.

4. **Request models** — change these fields and models only:
```python
class ChatRequest(BaseModel):
    message: str
    jurisdiction: JurisdictionCode = "IN"
    context: Optional[Dict[str, Any]] = None
    mask_pii: bool = True
    agentic: bool = True
    history: Optional[List[Dict[str, str]]] = None


class VoiceTurnRequest(BaseModel):
    audio_base64: str
    user_id: str = "USR-001"
    jurisdiction: JurisdictionCode = "IN"
    language_code: Optional[str] = "en-IN"


class RemediationExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    violation_id: str
    target_entity: str
    entity_id: str
    patch_payload: Dict[str, Any]
```

5. **Router** — add `router = APIRouter()` after the models. Change every `@app.get(` / `@app.post(` to `@router.get(` / `@router.post(`. Then change the handler signatures and the tenant/approver lines exactly as below. Bodies not shown stay as they are.
```python
@router.get("/healthz")
def healthz():  # body unchanged


@router.post("/v1/chat", response_model=AgentResponse)
async def chat_endpoint(
    req: ChatRequest,
    request: Request,
    auth: AuthContext = Depends(require(Permission.USE_COPILOT)),
):
    agent: SupervisorAgent = request.app.state.supervisor
    text_to_process = req.message
    if req.mask_pii:
        text_to_process, _ = pii_gateway.mask_text(req.message, tenant_id=auth.tenant_id)

    if req.agentic:
        response = await agent.process_agentic(
            message=text_to_process,
            jurisdiction=req.jurisdiction,
            context=req.context,
            tenant_id=auth.tenant_id,
            history=req.history,
        )
    else:
        response = agent.process_message(
            message=text_to_process,
            jurisdiction=req.jurisdiction,
            context=req.context,
        )

    if req.mask_pii:
        response.reply_text = pii_gateway.unmask_text(response.reply_text, tenant_id=auth.tenant_id)
    return response


@router.post("/v1/chat/stream")
async def chat_stream_endpoint(
    req: ChatRequest,
    request: Request,
    auth: AuthContext = Depends(require(Permission.USE_COPILOT)),
):
    agent: SupervisorAgent = request.app.state.supervisor

    async def event_generator():
        text_to_process = req.message
        if req.mask_pii:
            text_to_process, _ = pii_gateway.mask_text(req.message, tenant_id=auth.tenant_id)
        async for chunk in agent.stream_agentic(
            message=text_to_process,
            jurisdiction=req.jurisdiction,
            context=req.context,
            tenant_id=auth.tenant_id,
        ):
            yield f"data: {json.dumps(chunk)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.post("/v1/voice/transcribe")
async def voice_transcribe(req: VoiceTranscribeRequest, auth: AuthContext = Depends(require(Permission.USE_COPILOT))):
    # body unchanged


@router.post("/v1/voice/synthesize")
async def voice_synthesize(req: VoiceSynthesizeRequest, auth: AuthContext = Depends(require(Permission.USE_COPILOT))):
    # body unchanged


@router.post("/v1/voice/turn")
async def voice_turn(
    req: VoiceTurnRequest,
    request: Request,
    auth: AuthContext = Depends(require(Permission.USE_COPILOT)),
):
    agent: SupervisorAgent = request.app.state.supervisor
    # body unchanged, except: `supervisor.process_agentic(` -> `agent.process_agentic(` and
    # `tenant_id=x_tenant_id,` -> `tenant_id=auth.tenant_id,`


@router.post("/v1/schema/introspect")
def introspect_schema(req: IntrospectRequest, auth: AuthContext = Depends(require(Permission.MANAGE_SCHEMA))):
    # body unchanged


@router.post("/v1/schema/synthesize")
def synthesize_mapping(req: SynthesizeRequest, auth: AuthContext = Depends(require(Permission.MANAGE_SCHEMA))):
    # body unchanged, except: `tenant_id=x_tenant_id` -> `tenant_id=auth.tenant_id`


@router.post("/v1/schema/synthesize/agentic")
async def synthesize_mapping_agentic(req: SynthesizeRequest, auth: AuthContext = Depends(require(Permission.MANAGE_SCHEMA))):
    # body unchanged, except: `tenant_id=x_tenant_id` -> `tenant_id=auth.tenant_id`


@router.get("/v1/schema/mappings")
def list_schema_mappings(auth: AuthContext = Depends(require(Permission.MANAGE_SCHEMA))):
    return {"tenant_id": auth.tenant_id, "mappings": mapping_store.list_mappings(auth.tenant_id)}


@router.get("/v1/schema/mappings/{vendor}/{entity_type}")
def get_schema_mapping(vendor: str, entity_type: str, auth: AuthContext = Depends(require(Permission.MANAGE_SCHEMA))):
    mapping = mapping_store.load_mapping(auth.tenant_id, vendor, entity_type)
    # rest of body unchanged


@router.post("/v1/remediate/execute")
def execute_remediation(
    req: RemediationExecuteRequest,
    auth: AuthContext = Depends(
        require(Permission.EXECUTE_REMEDIATION, max_token_age_seconds=REMEDIATION_MAX_TOKEN_AGE_SECONDS)
    ),
):
    """Execute human-approved compliance remediation patch with signed host audit attribution headers."""
    headers = audit_gateway.create_attribution_headers(
        tenant_id=auth.tenant_id,
        agent_name="StatutoryComplianceAuditor",
        hitl_approver_id=auth.user_id,
        reasoning_payload={
            "violation_id": req.violation_id,
            "entity": req.target_entity,
            "entity_id": req.entity_id,
            "patch": req.patch_payload,
        },
    )
    return {
        "status": "APPROVED_AND_QUEUED",
        "violation_id": req.violation_id,
        "entity_id": req.entity_id,
        "patch_applied": req.patch_payload,
        "audit_tracing": headers.to_http_headers(),
    }


@router.get("/v1/compliance/diagnose")
@router.get("/api/compliance/agent/diagnose")
def get_compliance_diagnosis(jurisdiction: str = "AE", auth: AuthContext = Depends(require(Permission.VIEW_COMPLIANCE))):
    # body unchanged, except: `tenant_id=tenant_id,` -> `tenant_id=auth.tenant_id,`


@router.get("/v1/compliance/dashboard/live")
@router.get("/api/compliance/dashboard/live")
def get_compliance_dashboard_live(auth: AuthContext = Depends(require(Permission.VIEW_COMPLIANCE))):
    # body unchanged


@router.post("/api/compliance/agent/chat")
def legacy_chat_adapter(
    req: Dict[str, Any],
    request: Request,
    auth: AuthContext = Depends(require(Permission.USE_COPILOT)),
):
    msg = str(req.get("message", ""))
    resp = request.app.state.supervisor.process_message(
        message=msg,
        jurisdiction="AE" if "uae" in msg.lower() or "aed" in msg.lower() or "eosb" in msg.lower() else "IN",
        context=req.get("context", {}),
    )
    # return block unchanged


@router.post("/api/requisitions/generate-archetypes")
@router.post("/v1/requisitions/generate-archetypes")
def generate_requisition_archetypes(
    req: GenerateArchetypesRequest,
    auth: AuthContext = Depends(require(Permission.MANAGE_RECRUITMENT)),
):
    # body unchanged
```

6. **Factory** — append at the end of the module:
```python
async def _api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.body, headers=exc.headers)


async def _permission_denied_handler(_request: Request, exc: PermissionDenied) -> JSONResponse:
    return JSONResponse(status_code=403, content={"error": "FORBIDDEN", "permission": exc.permission.value})


def create_app(
    *,
    registry: TenantRegistry,
    jwks_cache: JwksCache,
    clock: Callable[[], float] = time.time,
    supervisor_agent: Optional[SupervisorAgent] = None,
    env: Optional[Mapping[str, str]] = None,
) -> FastAPI:
    env = os.environ if env is None else env
    docs = env.get(API_DOCS_ENV_VAR) == "true"
    application = FastAPI(
        title="Agentic HRMS AI Plugin Service",
        version="2.0.0",
        description="Universal, host-agnostic AI copilot and statutory compliance sidecar for HRMS suites.",
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    application.state.token_verifier = TokenVerifier(registry, jwks_cache, clock=clock)
    application.state.clock = clock
    application.state.supervisor = supervisor_agent or supervisor
    application.add_middleware(
        CORSMiddleware,
        allow_origins=registry.all_origins(),
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Tenant-Id"],
    )
    application.add_exception_handler(ApiError, _api_error_handler)
    application.add_exception_handler(PermissionDenied, _permission_denied_handler)
    application.include_router(router)
    return application


def _create_default_app() -> FastAPI:
    return create_app(
        registry=load_registry_from_env(),
        jwks_cache=JwksCache(make_httpx_fetcher(httpx.AsyncClient())),
    )


app = _create_default_app()
```
Remove unused imports that ruff reports (`HTTPException` stays only if still used by `introspect_schema` and `get_schema_mapping`).

- [ ] **Step 5: Rewrite the existing route tests** (they now need tokens; AM1 split)

`tests/unit/test_server_routes.py`:
```python
"""Integration tests for FastAPI REST routes and MCP Server."""

import pytest

from hrms_plugin.agents.supervisor import SupervisorAgent
from hrms_plugin.server.mcp_server import HrmsMcpServer
from tests.support.scripted_llm import final, tool_call


@pytest.fixture
def client(make_client):
    return make_client()


@pytest.fixture
def mcp_server():
    return HrmsMcpServer()


def test_healthz_endpoint(client):
    res = client.get("/healthz")
    assert res.status_code == 200
    assert res.json()["status"] == "healthy"


def test_chat_endpoint_payroll_routing_non_agentic(client, bearer):
    """AM1: original assertions, on the non-agentic path they were written for."""
    payload = {
        "message": "Calculate salary structure for annual CTC 1800000 INR with Aadhaar 9988 7766 5544",
        "jurisdiction": "IN",
        "context": {"annual_ctc": 1800000.0},
        "mask_pii": True,
        "agentic": False,
    }
    res = client.post("/v1/chat", json=payload, headers=bearer(roles=["hr_manager"]))
    assert res.status_code == 200
    data = res.json()
    assert data["intent"] == "PAYROLL_STRUCTURE"
    assert data["routed_agent"] == "StatutoryPayrollAgent"
    assert data["structured_data"]["annual_ctc"] == 1800000.0
    assert len(data["statutory_citations"]) > 0


def test_chat_endpoint_payroll_routing_agentic(make_client, bearer, scripted):
    """AM1: agentic path reports the intent of the tool it ran."""
    gateway, _ = scripted(tool_call("calculate_salary_structure", gross_or_ctc=1800000.0), final("Structured."))
    client = make_client(supervisor_agent=SupervisorAgent(llm_gateway=gateway))
    res = client.post("/v1/chat", headers=bearer(roles=["hr_manager"]),
                      json={"message": "Calculate salary structure for annual CTC 1800000", "jurisdiction": "IN"})
    assert res.status_code == 200
    data = res.json()
    assert data["intent"] == "PAYROLL_STRUCTURE"
    assert data["routed_agent"] == "AgenticBrain"
    assert data["tool_calls"][0]["arguments"]["jurisdiction"] == "IN"


def test_remediation_execute_endpoint(client, bearer):
    payload = {
        "violation_id": "VIO-PROB-EMP-401",
        "target_entity": "EMPLOYEE",
        "entity_id": "EMP-401",
        "patch_payload": {"probation_days": 180},
    }
    res = client.post("/v1/remediate/execute", json=payload, headers=bearer(sub="MGR-007", roles=["hr_manager"]))
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "APPROVED_AND_QUEUED"
    assert data["audit_tracing"]["X-HITL-Approver-Id"] == "MGR-007"
    assert data["audit_tracing"]["X-Agent-Name"] == "StatutoryComplianceAuditor"


def test_mcp_server_tool_listing_and_invocation(mcp_server):
    pass  # REPLACE with the original body of this test, copied verbatim from git (it does not use HTTP)


def test_generate_requisition_archetypes_endpoint(client, bearer):
    payload = {"prompt": "Senior Full Stack React and Python Engineer", "location": "Bangalore / Remote"}
    res = client.post("/api/requisitions/generate-archetypes", json=payload, headers=bearer(roles=["recruiter"]))
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert len(data["archetypes"]) == 3
    first = data["archetypes"][0]
    assert "jobData" in first
    assert "React" in first["jobData"]["requirements"] or "Python" in first["jobData"]["requirements"]
    assert first["jobData"]["location"] == "Bangalore / Remote"
    assert first["jobData"]["minSalary"] > 0
```
For `test_mcp_server_tool_listing_and_invocation`, copy the body verbatim with `git show HEAD:tests/unit/test_server_routes.py`. Leaving the `pass` line in place is a plan violation.

`tests/e2e/test_copilot_e2e.py` — change only the following; keep all other lines and assertions:
- Replace the imports and `client` fixture with:
  ```python
  import pytest

  from hrms_plugin.agents.supervisor import SupervisorAgent
  from tests.support.scripted_llm import final, tool_call


  @pytest.fixture
  def client(make_client):
      return make_client()
  ```
- `test_e2e_compliance_audit_and_remediation_lifecycle(make_client, bearer, scripted)`:
  - First line: `gateway, _ = scripted(tool_call("audit_statutory_compliance"), final("Violations found under UAE Federal Decree-Law No. 33 of 2021."))`.
  - Then: `client = make_client(supervisor_agent=SupervisorAgent(llm_gateway=gateway))`.
  - Chat call headers: `bearer(tenant="acme", roles=["hr_manager"])`, and remove `"tenant_id": "demo_corp",` from `context`.
  - In `rem_payload`, delete `approver_id` and `approver_role`.
  - Remediation call headers: `bearer(tenant="acme", sub="USR-MGR-501", roles=["hr_manager"])`.
  - Keep every assertion.
- `test_e2e_zero_shot_schema_adaptation_lifecycle(client, bearer)`: add `headers=bearer(roles=["platform_admin"])` to both posts.

`tests/unit/test_agentic_llm.py`:
- Delete `from hrms_plugin.server.app import app` and replace the `client` fixture with `def client(make_client): return make_client()`.
- Add `headers=bearer(roles=["platform_admin"])` to every `client.post(...)`, and add `bearer` to those tests' parameters.
- Drop `headers={"X-Tenant-Id": "demo_corp"}`, because its tenant `demo_corp` differs from the token's `acme` and would get 403 `TENANT_MISMATCH`, which is correct behaviour.

- [ ] **Step 6: Verify** — the new route tests and all rewritten tests pass. **The full suite must now be fully green** (0 failures), since the two original failures are fixed by AM1 and the e2e rewrite.

- [ ] **Step 7: Commit** — `feat(server): app factory with per-route permissions, strict request models, CORS allowlist, docs flag`

---

### Task 13: Tool permissions (spec §6.5)

**Files:**
- Modify: `hrms_plugin/agents/tools.py`, `hrms_plugin/agents/orchestrator.py`, `hrms_plugin/agents/supervisor.py` (`process_agentic`/`stream_agentic`), `hrms_plugin/server/app.py` (3 call sites)
- Modify: `tests/unit/test_orchestrator_dispatch.py`, `tests/unit/test_agentic_llm.py` (pass `auth`)
- Test: `tests/unit/test_tool_permissions.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Tools are offered and executed only with the caller's permission (spec §6.5)."""

import logging

import pytest

from hrms_plugin.agents.orchestrator import AgenticOrchestrator
from hrms_plugin.agents.tools import AgenticToolRegistry
from hrms_plugin.ai.llm import ToolDefinition
from hrms_plugin.agents.supervisor import UserIntent
from hrms_plugin.security.rbac import AuthContext, Permission as P, UserRole as R
from tests.support.scripted_llm import final, tool_call

TOOL_PERMISSIONS = {
    "calculate_salary_structure": P.VIEW_SALARY, "calculate_eosb_gratuity": P.VIEW_SALARY,
    "audit_offboarding_fnf_timeline": P.VIEW_SALARY, "compute_final_settlement": P.VIEW_SALARY,
    "audit_statutory_compliance": P.VIEW_COMPLIANCE, "evaluate_leave_request": P.APPLY_LEAVE,
    "detect_travel_anomaly": P.APPROVE_LEAVE, "score_candidate_ats": P.MANAGE_RECRUITMENT,
    "audit_job_description_bias": P.MANAGE_RECRUITMENT, "evaluate_onboarding_offer": P.MANAGE_RECRUITMENT,
    "generate_requisition_archetypes": P.MANAGE_RECRUITMENT, "verify_onboarding_documents": P.VIEW_EMPLOYEE,
    "search_labor_regulations": P.USE_COPILOT, "audit_travel_expense_claim": P.APPROVE_EXPENSE,
}


def auth(*roles):
    return AuthContext(user_id="u-1", tenant_id="acme", roles=frozenset(roles))


@pytest.mark.parametrize("tool,perm", sorted(TOOL_PERMISSIONS.items()))
def test_tool_permission_table(tool, perm):
    assert AgenticToolRegistry().permission_for(tool) is perm


def test_every_registered_tool_has_a_permission():
    reg = AgenticToolRegistry()
    assert {d.name for d in reg.get_definitions()} == set(TOOL_PERMISSIONS)


def test_register_without_permission_raises():
    with pytest.raises(TypeError):
        AgenticToolRegistry().register(ToolDefinition(name="x", description="", parameters={}), lambda: {},
                                       intent=UserIntent.GENERAL_HR)


def test_hr_admin_offered_exact_set():
    from hrms_plugin.security.rbac import ROLE_PERMISSIONS

    offered = {d.name for d in AgenticToolRegistry().definitions_for(auth(R.HR_ADMIN))}
    assert offered == {t for t, p in TOOL_PERMISSIONS.items() if p in ROLE_PERMISSIONS[R.HR_ADMIN]}


def test_employee_not_offered_audit():
    offered = {d.name for d in AgenticToolRegistry().definitions_for(auth(R.EMPLOYEE))}
    assert "audit_statutory_compliance" not in offered
    assert offered == {"evaluate_leave_request", "verify_onboarding_documents", "search_labor_regulations"}


async def test_unoffered_tool_call_is_blocked_and_logged(scripted, caplog):
    caplog.set_level(logging.WARNING)
    gateway, client = scripted(tool_call("audit_statutory_compliance"), final("ok"))
    orch = AgenticOrchestrator(llm_gateway=gateway, tool_registry=AgenticToolRegistry())
    resp = await orch.run(user_message="audit", jurisdiction="AE", auth=auth(R.EMPLOYEE))
    assert resp.structured_data is None
    assert resp.intent == UserIntent.GENERAL_HR
    assert "TOOL_NOT_PERMITTED" in client.calls[1]["messages"][-1].content
    rec = next(r for r in caplog.records if getattr(r, "reason", "") == "TOOL_NOT_PERMITTED")
    assert (rec.sub, rec.tenant, rec.tool) == ("u-1", "acme", "audit_statutory_compliance")


async def test_zero_roles_calls_llm_without_tools(scripted):
    gateway, client = scripted(final("hello"))
    orch = AgenticOrchestrator(llm_gateway=gateway, tool_registry=AgenticToolRegistry())
    await orch.run(user_message="hi", jurisdiction="AE", auth=auth())
    assert client.calls[0]["tools"] is None


async def test_stream_filters_and_rechecks(scripted):
    gateway, client = scripted(tool_call("audit_statutory_compliance"), final("ok"))
    orch = AgenticOrchestrator(llm_gateway=gateway, tool_registry=AgenticToolRegistry())
    events = [e async for e in orch.run_stream(user_message="a", jurisdiction="AE", auth=auth(R.EMPLOYEE))]
    offered = {t.name for t in client.calls[0]["tools"]}
    assert "audit_statutory_compliance" not in offered
    obs = next(e for e in events if e["event"] == "tool_observation")
    assert obs["data"]["output"] == {"error": "TOOL_NOT_PERMITTED", "tool": "audit_statutory_compliance"}


async def test_tenant_injected_from_auth(scripted):
    gateway, _ = scripted(tool_call("audit_statutory_compliance", tenant_id="evil"), final("ok"))
    orch = AgenticOrchestrator(llm_gateway=gateway, tool_registry=AgenticToolRegistry())
    resp = await orch.run(user_message="a", jurisdiction="AE", auth=auth(R.HR_ADMIN))
    assert resp.tool_calls[0]["arguments"]["tenant_id"] == "acme"
```

- [ ] **Step 2: Run** → `AttributeError: 'AgenticToolRegistry' object has no attribute 'permission_for'`.

- [ ] **Step 3: Implement**

`tools.py`:
- Imports: `from hrms_plugin.security.rbac import AuthContext, Permission, RbacPolicyEnforcer`.
- In `__init__`: `self._permissions: Dict[str, Permission] = {}` and `self._rbac = RbacPolicyEnforcer()`.
- Replace `register` and add three methods:
```python
def register(
    self, definition: ToolDefinition, handler: Callable[..., Any], *, intent: UserIntent, permission: Permission
) -> None:
    self._tools[definition.name] = definition
    self._handlers[definition.name] = handler
    self._intents[definition.name] = intent
    self._permissions[definition.name] = permission

def permission_for(self, name: str) -> Optional[Permission]:
    return self._permissions.get(name)

def is_permitted(self, name: str, auth: AuthContext) -> bool:
    permission = self._permissions.get(name)
    return permission is not None and self._rbac.is_authorized(auth, permission)

def definitions_for(self, auth: AuthContext) -> List[ToolDefinition]:
    return [d for name, d in self._tools.items() if self.is_permitted(name, auth)]
```
- Add `permission=Permission.<X>` to each of the 14 `register` calls per `TOOL_PERMISSIONS`.

`orchestrator.py`:
- Add `from hrms_plugin.security.rbac import AuthContext`.
- `run(self, user_message, jurisdiction="IN", context=None, history=None, *, auth: AuthContext)` and `run_stream(self, user_message, jurisdiction="IN", context=None, *, auth: AuthContext)`: remove the `tenant_id` parameter, set `tenant_id = auth.tenant_id` at the top of each, and replace `tools = self.tool_registry.get_definitions()` with `tools = self.tool_registry.definitions_for(auth) or None`.
- `_execute_tool_call` gains a parameter `auth: AuthContext` (both call sites pass `auth`). Insert after the `definition is None` branch:
```python
        elif not self.tool_registry.is_permitted(tc.name, auth):
            logger.warning(
                "tool call not permitted",
                extra={"reason": "TOOL_NOT_PERMITTED", "tenant": auth.tenant_id, "sub": auth.user_id, "tool": tc.name},
            )
            output = {"error": "TOOL_NOT_PERMITTED", "tool": tc.name}
            arguments, overrides = dict(tc.arguments), []
```

`supervisor.py`:
- `process_agentic(self, message, jurisdiction=Jurisdiction.INDIA, context=None, history=None, *, auth: AuthContext)` → `orchestrator.run(..., history=history, auth=auth)`.
- `stream_agentic(self, message, jurisdiction=Jurisdiction.INDIA, context=None, *, auth: AuthContext)` → `orchestrator.run_stream(..., auth=auth)`.
- Import `AuthContext` from `hrms_plugin.security.rbac`.

`app.py`: in `chat_endpoint`, `chat_stream_endpoint` and `voice_turn`, replace `tenant_id=auth.tenant_id` in the `process_agentic` / `stream_agentic` calls with `auth=auth`.

Existing tests:
- `tests/unit/test_orchestrator_dispatch.py`: in `_run` and `_stream`, replace `kw.setdefault("tenant_id", "acme")` with `kw.setdefault("auth", AuthContext(user_id="t", tenant_id="acme", roles=frozenset({UserRole.SUPER_ADMIN})))`. Change `test_tenant_id_is_injected_from_request` to pass `auth=AuthContext(user_id="t", tenant_id="acme", roles=frozenset({UserRole.SUPER_ADMIN}))` instead of `tenant_id="acme"`, and add the import.
- `tests/unit/test_agentic_llm.py`: in `orchestrator.run(...)`, `orchestrator.run_stream(...)`, `supervisor.process_agentic(...)` and `supervisor.stream_agentic(...)`, replace any `tenant_id=` argument with `auth=ADMIN` (or add `auth=ADMIN`). Define `ADMIN = AuthContext(user_id="test-admin", tenant_id="acme", roles=frozenset({UserRole.SUPER_ADMIN}))` at module top.

- [ ] **Step 4: Verify** — fully green.

- [ ] **Step 5: Commit** — `feat(agents): offer and execute tools only with the caller's permission`

---

### Task 14: Permission check on the keyword-routed path (spec §6.5a)

**Files:**
- Modify: `hrms_plugin/agents/supervisor.py`, `hrms_plugin/server/app.py` (2 call sites), `tests/unit/test_supervisor.py`
- Test: `tests/unit/test_process_message_permissions.py`

- [ ] **Step 1: Write the failing tests**

```python
"""process_message enforces the same permissions as the tools (spec §6.5a)."""

import pytest

from hrms_plugin.agents.supervisor import SupervisorAgent, UserIntent, required_permission
from hrms_plugin.security.rbac import AuthContext, Permission as P, PermissionDenied, UserRole as R

SETTLEMENT_MSG = "Compute final settlement for exiting employee"
ONBOARD_DOCS_MSG = "Verify onboarding documents KYC for new joiner"
ONBOARD_OFFER_MSG = "Evaluate onboarding offer for new joiner"


def auth(*roles):
    return AuthContext(user_id="u", tenant_id="acme", roles=frozenset(roles))


@pytest.mark.parametrize("intent", list(UserIntent))
def test_every_intent_has_a_permission(intent):
    assert isinstance(required_permission(intent, ""), P)


def test_onboarding_splits_by_action():
    assert required_permission(UserIntent.ONBOARDING, "verify kyc document") is P.VIEW_EMPLOYEE
    assert required_permission(UserIntent.ONBOARDING, "evaluate the offer") is P.MANAGE_RECRUITMENT


@pytest.mark.parametrize("msg,intent", [
    (SETTLEMENT_MSG, UserIntent.OFFBOARDING_FNF),
    (ONBOARD_DOCS_MSG, UserIntent.ONBOARDING),
    (ONBOARD_OFFER_MSG, UserIntent.ONBOARDING),
])
def test_precondition_messages_classify_as_expected(msg, intent):
    assert SupervisorAgent().classify_intent(msg)[0] == intent


def test_employee_denied_final_settlement():
    with pytest.raises(PermissionDenied) as info:
        SupervisorAgent().process_message(SETTLEMENT_MSG, jurisdiction="AE", auth=auth(R.EMPLOYEE))
    assert info.value.permission is P.VIEW_SALARY


def test_hr_admin_allowed_final_settlement():
    assert SupervisorAgent().process_message(SETTLEMENT_MSG, jurisdiction="AE", auth=auth(R.HR_ADMIN)).reply_text


def test_employee_allowed_onboarding_documents_but_not_offer():
    agent = SupervisorAgent()
    assert agent.process_message(ONBOARD_DOCS_MSG, jurisdiction="IN", auth=auth(R.EMPLOYEE)).reply_text
    with pytest.raises(PermissionDenied) as info:
        agent.process_message(ONBOARD_OFFER_MSG, jurisdiction="IN", auth=auth(R.EMPLOYEE))
    assert info.value.permission is P.MANAGE_RECRUITMENT


def test_auth_is_required():
    with pytest.raises(TypeError):
        SupervisorAgent().process_message(SETTLEMENT_MSG, jurisdiction="AE")


def test_http_non_agentic_and_legacy_paths_return_403(make_client, bearer):
    client = make_client()
    h = bearer(roles=["staff"])
    res = client.post("/v1/chat", headers=h, json={"message": SETTLEMENT_MSG, "jurisdiction": "AE", "agentic": False})
    assert res.status_code == 403 and res.json() == {"error": "FORBIDDEN", "permission": "VIEW_SALARY"}
    res = client.post("/api/compliance/agent/chat", headers=h, json={"message": SETTLEMENT_MSG})
    assert res.status_code == 403


def test_http_hr_admin_non_agentic_ok(make_client, bearer):
    res = make_client().post("/v1/chat", headers=bearer(roles=["hr_manager"]),
                             json={"message": SETTLEMENT_MSG, "jurisdiction": "AE", "agentic": False})
    assert res.status_code == 200
```
If a precondition test fails (a message classifies differently), adjust **only the message text** until it classifies as stated. Record the change in the TDD log.

- [ ] **Step 2: Run** → `ImportError: cannot import name 'required_permission'`.

- [ ] **Step 3: Implement** — in `supervisor.py`:
```python
from hrms_plugin.security.rbac import AuthContext, Permission, PermissionDenied, RbacPolicyEnforcer

ONBOARDING_DOCUMENT_KEYWORDS = ("document", "kyc", "pan", "aadhaar", "emirates id")

INTENT_PERMISSIONS: Dict[UserIntent, Permission] = {
    UserIntent.PAYROLL_STRUCTURE: Permission.VIEW_SALARY,
    UserIntent.PAYROLL_EOSB: Permission.VIEW_SALARY,
    UserIntent.COMPLIANCE_AUDIT: Permission.VIEW_COMPLIANCE,
    UserIntent.LEAVE_APPLY: Permission.APPLY_LEAVE,
    UserIntent.ATTENDANCE_ANOMALY: Permission.APPROVE_LEAVE,
    UserIntent.RECRUITMENT_ATS: Permission.MANAGE_RECRUITMENT,
    UserIntent.RECRUITMENT_BIAS: Permission.MANAGE_RECRUITMENT,
    UserIntent.RECRUITMENT_REQUISITION: Permission.MANAGE_RECRUITMENT,
    UserIntent.OFFBOARDING_FNF: Permission.VIEW_SALARY,
    UserIntent.TRAVEL_EXPENSE: Permission.APPROVE_EXPENSE,
    UserIntent.LEGAL_QNA: Permission.USE_COPILOT,
    UserIntent.GENERAL_HR: Permission.USE_COPILOT,
}


def required_permission(intent: UserIntent, message_lower: str) -> Permission:
    """Permission for the action process_message will take for this intent and message."""
    if intent == UserIntent.ONBOARDING:
        if any(k in message_lower for k in ONBOARDING_DOCUMENT_KEYWORDS):
            return Permission.VIEW_EMPLOYEE
        return Permission.MANAGE_RECRUITMENT
    return INTENT_PERMISSIONS[intent]
```
- In `SupervisorAgent.__init__`, add `self._rbac = RbacPolicyEnforcer()`.
- Change the signature to `def process_message(self, message, jurisdiction=Jurisdiction.INDIA, context=None, *, auth: AuthContext) -> AgentResponse:`.
- Directly after `msg = message.lower()`, insert:
```python
        permission = required_permission(intent, msg)
        if not self._rbac.is_authorized(auth, permission):
            raise PermissionDenied(permission)
```
- Replace the onboarding branch's inline list `["document", "kyc", "pan", "aadhaar", "emirates id"]` with `ONBOARDING_DOCUMENT_KEYWORDS`.

`app.py`: add `auth=auth` to the two `process_message(` calls (`chat_endpoint` and `legacy_chat_adapter`).

`tests/unit/test_supervisor.py`: add at module top
```python
from hrms_plugin.security.rbac import AuthContext, UserRole

ADMIN = AuthContext(user_id="test-admin", tenant_id="acme", roles=frozenset({UserRole.SUPER_ADMIN}))
```
and add `auth=ADMIN` to every `supervisor.process_message(` call. Keep all assertions.

- [ ] **Step 4: Verify** — fully green, and `grep -rn "process_message(" hrms_plugin tests` shows every call passing `auth=`.

- [ ] **Step 5: Commit** — `feat(agents): enforce tool permissions on the keyword-routed path`

---

### Task 15: Acceptance (spec §8, AM4)

- [ ] **Step 1:** `.venv/bin/ruff check .` → exit 0.
- [ ] **Step 2:** `.venv/bin/python -m pytest -q` → 0 failed (Python 3.11).
- [ ] **Step 3:** Python 3.12:
  ```bash
  uv venv -q -p 3.12 .venv312 && uv pip install -q -p .venv312/bin/python -e '.[dev]'
  .venv312/bin/python -m pytest -q
  ```
  → 0 failed.
- [ ] **Step 4:** Startup refusal with no registry:
  ```bash
  env -u HRMS_TENANT_REGISTRY .venv/bin/python -m hrms_plugin.server
  ```
  → exits non-zero, and stderr names `HRMS_TENANT_REGISTRY`.
- [ ] **Step 5:** Live boot with the fixture registry:
  ```bash
  HRMS_TENANT_REGISTRY=tests/fixtures/tenant_registry.yaml HRMS_DATA_DIR=$(mktemp -d) .venv/bin/python -m hrms_plugin.server &
  ```
  Then:
  - `curl -s localhost:8000/healthz` → healthy.
  - `curl -s -o /dev/null -w '%{http_code}' -X POST localhost:8000/v1/remediate/execute -H 'Content-Type: application/json' -d '{}'` → `401`.

  Stop the server.
- [ ] **Step 6:** Docker, if available: build and run as in the CI job; `id -u` → 10001. If unavailable, write that in the TDD log.
- [ ] **Step 7:** Confirm every behaviour row in spec §5–§6 has a red→green entry in `docs/superpowers/plans/2026-10-28-tdd-log.md`.
- [ ] **Step 8: Commit** — `chore: acceptance evidence for stabilise + auth`

---

### Task 16: Independent strict verification by `agy` (user instruction)

- [ ] **Step 1:** Write `/private/tmp/claude-501/-Users-satosh-Agentic-Plugin-main/0caaed59-06ce-4abb-af96-b5e91029ba84/scratchpad/agy-verify-brief.md`. It must contain:
  - The paths to the spec, this plan and the TDD log.
  - `git diff eab02e5..HEAD --stat` and the base commit hash.
  - Instructions:
    - Verify every spec requirement (§5, §6, §9) against the code and tests, and run the acceptance commands themselves.
    - Attack the implementation: auth bypasses, tenant confusion, algorithm confusion, timing-boundary errors, missing negative or boundary tests, tests that pass vacuously, deviations from the spec.
    - Report findings **only** as a JSON array `[{"id","severity":"HIGH|MEDIUM|LOW","file","line","requirement","finding","evidence","reproduce"}]` in `docs/superpowers/plans/2026-10-28-agy-findings.json`, plus a one-line verdict.
    - Make no code changes.
- [ ] **Step 2:** Run (Bash timeout 600000):
  ```bash
  agy --dangerously-skip-permissions --model gemini-3.8-flash-high -p "$(cat <brief path>)" > <scratchpad>/agy-verify.log 2>&1; echo "exit=$?"
  ```
- [ ] **Step 3:** Judge success only by whether `docs/superpowers/plans/2026-10-28-agy-findings.json` exists and is valid JSON (`.venv/bin/python -m json.tool <file>` exits 0). Do not rely on agy's narrative output.
- [ ] **Step 4:** If the file is missing or invalid, retry once with `gemini-3.8-flash-high` and a shorter brief. If that also fails, fall back to a Claude `model: sonnet` subagent with the same brief (DEV_LOOP cap: 3 attempts). Then report.

### Task 17: Fix every agy finding (TDD)

For each finding, in severity order:
- [ ] Reproduce it with a failing test, or show with code that it is not real (quote the line). A disputed finding is reported to the user with its evidence; it is never silently dropped.
- [ ] Fix with the minimum change, run the full suite, and commit `fix: <finding id> <summary>`.
- [ ] Record each finding as fixed, disputed or needs-user-decision in `docs/superpowers/plans/2026-10-28-agy-findings-resolution.md`.
- [ ] A finding that needs a decision only the user can make (spec change, product choice) is written as `NEEDS_HUMAN` and parked, not guessed.
- [ ] Per the DEV_LOOP cap, there is no second full `agy` review round. Each fix is proven by its own red→green test and the acceptance commands (Task 15, steps 1–3).
