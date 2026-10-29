"""Universal Multi-Provider LLM Gateway for HRMS Agentic Plugin.

Provides an enterprise-grade, resilient abstraction over major LLM providers:
- OpenAI, Groq, OpenRouter, NVIDIA NIM, Local vLLM/Ollama (OpenAI-compatible)
- Anthropic Claude Messages API
- Google Gemini REST API
- Deterministic Offline Fake Provider (for zero-latency, zero-cost tests & local runs)

Invariants:
- Unconditional PII Masking: Redacts PII before prompts leave process, re-hydrates on egress.
- Task-based determinism: Scoring and evaluation pinned to temperature 0.0.
- Autonomous Tool Calling & Streaming SSE support.
"""

from __future__ import annotations

import asyncio
import email.utils
import hashlib
import json
import logging
import math
import os
import re
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional

import anthropic
import httpx

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from hrms_plugin.security.pii import PiiEgressBlocked, PiiSession, StreamRestorer

logger = logging.getLogger(__name__)


class LLMProvider(str, Enum):
    FAKE = "fake"
    OPENAI = "openai"
    OPENROUTER = "openrouter"
    GROQ = "groq"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"
    NVIDIA = "nvidia"


class TaskClass(str, Enum):
    EXTRACTION = "EXTRACTION"
    CLASSIFICATION = "CLASSIFICATION"
    SCORING = "SCORING"  # Deterministic (temp=0.0)
    EVALUATION = "EVALUATION"  # Deterministic (temp=0.0)
    SYNTHESIS = "SYNTHESIS"
    DRAFTING = "DRAFTING"
    POLICY_QA = "POLICY_QA"
    AGENT_REACT = "AGENT_REACT"


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: Dict[str, Any]

    def to_openai_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def to_anthropic_schema(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }

    def to_gemini_schema(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class ChatMessage:
    role: str  # "system", "user", "assistant", "tool"
    content: str
    tool_calls: Optional[List[ToolCall]] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None
    # Provider-native assistant content, replayed unchanged (keeps signed thinking blocks valid) — piece E2.
    raw_content: Optional[List[Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            data["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": json.dumps(tc.arguments, default=str)},
                }
                for tc in self.tool_calls
            ]
        if self.tool_call_id:
            data["tool_call_id"] = self.tool_call_id
        if self.name:
            data["name"] = self.name
        return data


@dataclass
class LLMResponse:
    content: str
    thought_process: Optional[str] = None
    tool_calls: List[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: Dict[str, int] = field(default_factory=dict)
    model: str = "unknown"
    provider: str = "fake"
    raw_content: Optional[List[Any]] = None


@dataclass
class LLMConfig:
    provider: LLMProvider = LLMProvider.FAKE
    model: str = "fake-agent-v1"
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    temperature: float = 0.2
    max_tokens: int = 4096
    effort: str = "medium"
    timeout_seconds: float = 45.0


class LLMNotConfigured(Exception):
    """No usable LLM provider configuration; LLM-backed routes must fail loudly (piece E6)."""


ANTHROPIC_DEFAULT_MODEL = "claude-opus-5-5"
ANTHROPIC_MAX_TOKENS = 16000
ANTHROPIC_FALLBACK_BETA = "server-side-fallback-2026-07-01"
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
REFUSAL_TEXT = "This request was declined by the model provider's safety system."
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504, 529})
MAX_RETRIES = 2
MAX_RETRY_DELAY_SECONDS = 30.0

Sleep = Callable[[float], Awaitable[None]]


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    """Seconds to wait: Retry-After (delta-seconds or HTTP-date) when usable, else exponential backoff; ≤ 30 s."""
    backoff = 0.5 * 2**attempt
    retry_after = (response.headers.get("retry-after") or "").strip()
    delay = backoff
    if retry_after:
        try:
            seconds = float(retry_after)
            if math.isfinite(seconds) and seconds >= 0:
                delay = seconds
        except ValueError:
            try:
                when = email.utils.parsedate_to_datetime(retry_after)
                delay = max(0.0, when.timestamp() - time.time())
            except (TypeError, ValueError):
                pass
    return min(delay, MAX_RETRY_DELAY_SECONDS)


async def _post_with_retry(
    http: httpx.AsyncClient, url: str, *, headers: Dict[str, str], payload: Dict[str, Any], sleep: Sleep
) -> httpx.Response:
    """POST, retrying 429/5xx/529 up to MAX_RETRIES times with backoff that honours Retry-After (piece E7)."""
    attempt = 0
    while True:
        response = await http.post(url, headers=headers, json=payload)
        if response.status_code in RETRY_STATUSES and attempt < MAX_RETRIES:
            await sleep(_retry_delay(response, attempt))
            attempt += 1
            continue
        response.raise_for_status()
        return response


class BaseLLMClient(ABC):
    """Abstract client contract for LLM backends."""

    def __init__(self, config: LLMConfig):
        self.config = config

    async def aclose(self) -> None:
        """Release network resources owned by this client (no-op by default)."""

    @abstractmethod
    async def chat(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[ToolDefinition]] = None,
        temperature: Optional[float] = None,
    ) -> LLMResponse:
        pass

    @abstractmethod
    async def chat_stream(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[ToolDefinition]] = None,
        temperature: Optional[float] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        pass


class FakeLLMClient(BaseLLMClient):
    """Deterministic offline simulator with smart tool call & synthesis mocking."""

    async def chat(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[ToolDefinition]] = None,
        temperature: Optional[float] = None,
    ) -> LLMResponse:
        await asyncio.sleep(0.01)  # Simulate non-blocking micro-tick
        last_msg = messages[-1] if messages else ChatMessage(role="user", content="")
        user_text = ""
        for m in reversed(messages):
            if m.role == "user":
                user_text = m.content
                break

        user_lower = user_text.lower()

        # Check if previous message was a tool result
        if last_msg.role == "tool":
            # Synthesize final answer from tool result
            return LLMResponse(
                content=(
                    f"Based on the analysis: {last_msg.content}. "
                    "The records have been verified against statutory requirements."
                ),
                thought_process="Observed tool result and synthesized final compliant answer.",
                tool_calls=[],
                finish_reason="stop",
                model="fake-agentic-v1",
                provider="fake",
            )

        # Autonomous tool selection simulation if tools are provided
        if tools:
            tool_names = {t.name: t for t in tools}

            # Compliance audit trigger
            if (
                any(k in user_lower for k in ["audit", "compliance", "violation", "wps", "probation"])
                and "audit_statutory_compliance" in tool_names
            ):
                return LLMResponse(
                    content="",
                    thought_process=(
                        "User requested compliance audit. Invoking audit_statutory_compliance "
                        "tool to verify labor records."
                    ),
                    tool_calls=[
                        ToolCall(
                            id=f"call_{uuid.uuid4().hex[:8]}",
                            name="audit_statutory_compliance",
                            arguments={
                                "jurisdiction": "AE" if any(u in user_lower for u in ["uae", "dubai", "aed"]) else "IN"
                            },
                        )
                    ],
                    finish_reason="tool_calls",
                    model="fake-agentic-v1",
                    provider="fake",
                )

            # Payroll structure trigger
            if (
                any(k in user_lower for k in ["salary", "ctc", "take home", "gross to net"])
                and "calculate_salary_structure" in tool_names
            ):
                return LLMResponse(
                    content="",
                    thought_process="User requested salary structuring. Calling calculate_salary_structure tool.",
                    tool_calls=[
                        ToolCall(
                            id=f"call_{uuid.uuid4().hex[:8]}",
                            name="calculate_salary_structure",
                            arguments={
                                "gross_or_ctc": 12000.0,
                                "jurisdiction": "AE" if any(u in user_lower for u in ["uae", "dubai", "aed"]) else "IN",
                            },
                        )
                    ],
                    finish_reason="tool_calls",
                    model="fake-agentic-v1",
                    provider="fake",
                )

            # EOSB Gratuity trigger
            if (
                any(k in user_lower for k in ["eosb", "gratuity", "end of service"])
                and "calculate_eosb_gratuity" in tool_names
            ):
                return LLMResponse(
                    content="",
                    thought_process="User requested End of Service Gratuity. Invoking calculate_eosb_gratuity tool.",
                    tool_calls=[
                        ToolCall(
                            id=f"call_{uuid.uuid4().hex[:8]}",
                            name="calculate_eosb_gratuity",
                            arguments={"basic_wage": 15000.0, "tenure_years": 3.5, "jurisdiction": "AE"},
                        )
                    ],
                    finish_reason="tool_calls",
                    model="fake-agentic-v1",
                    provider="fake",
                )

            # Leave evaluation trigger
            if any(k in user_lower for k in ["leave", "vacation"]) and "evaluate_leave_request" in tool_names:
                return LLMResponse(
                    content="",
                    thought_process="User requested leave evaluation. Invoking evaluate_leave_request tool.",
                    tool_calls=[
                        ToolCall(
                            id=f"call_{uuid.uuid4().hex[:8]}",
                            name="evaluate_leave_request",
                            arguments={"employee_id": "EMP-001", "leave_type": "ANNUAL", "requested_days": 2.0},
                        )
                    ],
                    finish_reason="tool_calls",
                    model="fake-agentic-v1",
                    provider="fake",
                )

        # Standard direct response
        return LLMResponse(
            content=(
                f"Agent response to: '{user_text}'. "
                "All statutory rules and organizational policies have been accounted for."
            ),
            thought_process="Analyzed natural language intent and determined direct compliant response.",
            tool_calls=[],
            finish_reason="stop",
            model="fake-agentic-v1",
            provider="fake",
        )

    async def chat_stream(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[ToolDefinition]] = None,
        temperature: Optional[float] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        resp = await self.chat(messages, tools, temperature)
        if resp.thought_process:
            yield {"event": "thinking", "data": resp.thought_process}
        if resp.tool_calls:
            for tc in resp.tool_calls:
                yield {
                    "event": "tool_call",
                    "data": {"id": tc.id, "name": tc.name, "arguments": tc.arguments},
                }
        else:
            words = resp.content.split(" ")
            for w in words:
                yield {"event": "token", "data": w + " "}
                await asyncio.sleep(0.01)
        yield {"event": "done", "data": {"finish_reason": resp.finish_reason}}


class OpenAICompatibleClient(BaseLLMClient):
    """Client for OpenAI, Groq, OpenRouter, NVIDIA NIM, and local OpenAI-compatible engines."""

    def __init__(
        self, config: LLMConfig, http_client: Optional[httpx.AsyncClient] = None, sleep: Sleep = asyncio.sleep
    ):
        super().__init__(config)
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=config.timeout_seconds)
        self._sleep = sleep
        self.base_url = (config.base_url or "https://api.openai.com/v1").rstrip("/")
        if config.provider == LLMProvider.GROQ and not config.base_url:
            self.base_url = "https://api.groq.com/openai/v1"
        elif config.provider == LLMProvider.OPENROUTER and not config.base_url:
            self.base_url = "https://openrouter.ai/api/v1"
        elif config.provider == LLMProvider.NVIDIA and not config.base_url:
            self.base_url = "https://integrate.api.nvidia.com/v1"

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def chat(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[ToolDefinition]] = None,
        temperature: Optional[float] = None,
    ) -> LLMResponse:
        payload: Dict[str, Any] = {
            "model": self.config.model,
            "messages": [m.to_dict() for m in messages],
            "temperature": temperature if temperature is not None else self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }
        if tools:
            payload["tools"] = [t.to_openai_schema() for t in tools]
            payload["tool_choice"] = "auto"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key or ''}",
        }

        resp = await _post_with_retry(
            self._http, f"{self.base_url}/chat/completions", headers=headers, payload=payload, sleep=self._sleep
        )
        data = resp.json()

        choice = data["choices"][0]
        msg = choice.get("message", {})
        content = msg.get("content") or ""
        tool_calls_data = msg.get("tool_calls", [])

        parsed_tool_calls: List[ToolCall] = []
        for tc in tool_calls_data:
            fn = tc.get("function", {})
            fn_args_raw = fn.get("arguments", "{}")
            try:
                fn_args = json.loads(fn_args_raw) if isinstance(fn_args_raw, str) else fn_args_raw
            except Exception:
                fn_args = {"raw": fn_args_raw}

            parsed_tool_calls.append(
                ToolCall(
                    id=tc.get("id", str(uuid.uuid4())),
                    name=fn.get("name", "unknown"),
                    arguments=fn_args,
                )
            )

        return LLMResponse(
            content=content,
            thought_process=msg.get("reasoning_content") or choice.get("thought"),
            tool_calls=parsed_tool_calls,
            finish_reason=choice.get("finish_reason", "stop"),
            usage=data.get("usage", {}),
            model=data.get("model", self.config.model),
            provider=self.config.provider.value,
        )

    async def chat_stream(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[ToolDefinition]] = None,
        temperature: Optional[float] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        payload: Dict[str, Any] = {
            "model": self.config.model,
            "messages": [m.to_dict() for m in messages],
            "temperature": temperature if temperature is not None else self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "stream": True,
        }
        if tools:
            payload["tools"] = [t.to_openai_schema() for t in tools]

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key or ''}",
        }

        if True:  # shared client (piece E7); streams are not retried once started
            async with self._http.stream(
                "POST", f"{self.base_url}/chat/completions", headers=headers, json=payload
            ) as stream:
                stream.raise_for_status()
                async for line in stream.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    line_data = line[6:].strip()
                    if line_data == "[DONE]":
                        yield {"event": "done", "data": {"finish_reason": "stop"}}
                        break
                    try:
                        chunk = json.loads(line_data)
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        if delta.get("content"):
                            yield {"event": "token", "data": delta["content"]}
                        if delta.get("tool_calls"):
                            yield {"event": "tool_call_delta", "data": delta["tool_calls"]}
                    except Exception:
                        continue


class AnthropicClient(BaseLLMClient):
    """Claude via the official Anthropic SDK (piece E1). Follows the bundled claude-api reference:
    adaptive thinking (parameter omitted), effort via output_config, no temperature, server-side refusal fallback.
    """

    def __init__(self, config: LLMConfig, http_client: Any = None):
        super().__init__(config)
        if config.effort not in EFFORT_LEVELS:
            raise LLMNotConfigured(f"effort must be one of {EFFORT_LEVELS}, got {config.effort!r}")
        kwargs: Dict[str, Any] = {"api_key": config.api_key, "max_retries": MAX_RETRIES}
        if http_client is not None:
            kwargs["http_client"] = http_client
        self._client = anthropic.AsyncAnthropic(**kwargs)

    async def aclose(self) -> None:
        await self._client.close()

    @staticmethod
    def _convert(messages: List[ChatMessage]) -> tuple:
        system_parts: List[str] = []
        out: List[Dict[str, Any]] = []
        open_tool_ids: set = set()  # tool_use ids of the most recent assistant turn
        for m in messages:
            if m.role == "system":
                if m.content:
                    system_parts.append(m.content)
            elif m.role == "tool":
                if m.tool_call_id not in open_tool_ids:
                    raise ValueError(f"tool result without a matching tool call: {m.tool_call_id!r}")
                block = {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content or ""}
                previous = out[-1] if out else None
                if (
                    previous is not None
                    and previous["role"] == "user"
                    and isinstance(previous["content"], list)
                    and all(b.get("type") == "tool_result" for b in previous["content"])
                ):
                    previous["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
            elif m.role == "assistant":
                if not out:
                    raise ValueError("first message must be from the user")
                if m.raw_content is not None:
                    content: Any = m.raw_content
                    open_tool_ids = {
                        (b.get("id") if isinstance(b, dict) else getattr(b, "id", None))
                        for b in m.raw_content
                        if (b.get("type") if isinstance(b, dict) else getattr(b, "type", None)) == "tool_use"
                    }
                else:
                    content = [{"type": "text", "text": m.content}] if m.content else []
                    content += [
                        {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments}
                        for tc in (m.tool_calls or [])
                    ]
                    open_tool_ids = {tc.id for tc in (m.tool_calls or [])}
                if content:
                    out.append({"role": "assistant", "content": content})
            elif m.content:  # user turn; empty user text is never sent
                out.append({"role": "user", "content": m.content})
        return ("\n".join(system_parts) or None), out

    async def chat(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[ToolDefinition]] = None,
        temperature: Optional[float] = None,  # accepted for interface parity; never sent (400 since 2026-10-21)
    ) -> LLMResponse:
        system, converted = self._convert(messages)
        kwargs: Dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "messages": converted,
            "output_config": {"effort": self.config.effort},
            "betas": [ANTHROPIC_FALLBACK_BETA],
            "fallbacks": "default",
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = [t.to_anthropic_schema() for t in tools]
        response = await self._client.beta.messages.create(**kwargs)
        usage = {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens}
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None)
            logger.warning("LLM refusal", extra={"reason": "LLM_REFUSAL", "category": category})
            return LLMResponse(content=REFUSAL_TEXT, finish_reason="refusal", usage=usage,
                               model=response.model, provider="anthropic")
        text = "".join(b.text for b in response.content if b.type == "text")
        tool_calls = [
            ToolCall(id=b.id, name=b.name, arguments=dict(b.input)) for b in response.content if b.type == "tool_use"
        ]
        return LLMResponse(
            content=text,
            tool_calls=tool_calls,
            finish_reason=response.stop_reason or "end_turn",
            usage=usage,
            model=response.model,
            provider="anthropic",
            raw_content=list(response.content),
        )

    async def chat_stream(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[ToolDefinition]] = None,
        temperature: Optional[float] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        resp = await self.chat(messages, tools, temperature)
        if resp.content:
            yield {"event": "token", "data": resp.content}
        yield {"event": "done", "data": {"finish_reason": resp.finish_reason}}


class GeminiClient(BaseLLMClient):
    """Client for Google Gemini REST API."""

    def __init__(
        self, config: LLMConfig, http_client: Optional[httpx.AsyncClient] = None, sleep: Sleep = asyncio.sleep
    ):
        super().__init__(config)
        self.model = config.model or "gemini-2.5-flash"
        self.api_key = config.api_key or ""
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=config.timeout_seconds)
        self._sleep = sleep

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def chat(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[ToolDefinition]] = None,
        temperature: Optional[float] = None,
    ) -> LLMResponse:
        contents = []
        system_instruction = None

        for m in messages:
            if m.role == "system":
                system_instruction = {"parts": [{"text": m.content}]}
            elif m.role == "tool":
                try:
                    res_obj = json.loads(m.content)
                except Exception:
                    res_obj = {"result": m.content}
                if not isinstance(res_obj, dict):
                    res_obj = {"output": res_obj}
                contents.append({
                    "role": "function",
                    "parts": [{
                        "functionResponse": {
                            "name": m.name or "tool_result",
                            "response": res_obj,
                        }
                    }],
                })
            elif m.role == "assistant" and m.tool_calls:
                parts: List[Dict[str, Any]] = []
                if m.content:
                    parts.append({"text": m.content})
                for tc in m.tool_calls:
                    parts.append({
                        "functionCall": {
                            "name": tc.name,
                            "args": tc.arguments,
                        }
                    })
                contents.append({"role": "model", "parts": parts})
            else:
                role = "user" if m.role == "user" else "model"
                contents.append({"role": role, "parts": [{"text": m.content}]})

        payload: Dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature if temperature is not None else self.config.temperature,
                "maxOutputTokens": self.config.max_tokens,
            },
        }
        if system_instruction:
            payload["system_instruction"] = system_instruction
        if tools:
            payload["tools"] = [{"functionDeclarations": [t.to_gemini_schema() for t in tools]}]

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        resp = await _post_with_retry(
            self._http, url, headers={"x-goog-api-key": self.api_key}, payload=payload, sleep=self._sleep
        )
        data = resp.json()

        candidates = data.get("candidates", [{}])
        cand = candidates[0] if candidates else {}
        parts = cand.get("content", {}).get("parts", [])

        content = ""
        tool_calls: List[ToolCall] = []
        for p in parts:
            if "text" in p:
                content += p["text"]
            elif "functionCall" in p:
                fc = p["functionCall"]
                tool_calls.append(
                    ToolCall(
                        id=str(uuid.uuid4()),
                        name=fc.get("name", ""),
                        arguments=fc.get("args", {}),
                    )
                )

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=cand.get("finishReason", "STOP"),
            model=self.model,
            provider="gemini",
        )

    async def chat_stream(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[ToolDefinition]] = None,
        temperature: Optional[float] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        resp = await self.chat(messages, tools, temperature)
        yield {"event": "token", "data": resp.content}
        yield {"event": "done", "data": {"finish_reason": resp.finish_reason}}


class LLMGateway:
    """The central enterprise gateway managing LLM interactions, PII safety, and multi-tenancy."""

    def __init__(self, config: Optional[LLMConfig] = None, egress_log: Any = None, policy_resolver: Any = None):
        """Gateways built from the environment (every production path) always enforce the tenant LLM policy and
        write the egress log (piece G). Explicit-config gateways (tests, embedding) do so when given them."""
        from_env = config is None
        self.config = config or self._resolve_config_from_env()
        self.client = self._create_client(self.config)
        if from_env:
            from hrms_plugin.governance.egress_log import EgressLog
            from hrms_plugin.governance.llm_policy import env_policy_resolver
            from hrms_plugin.storage.sqlite_store import SqliteKeyValueStore

            data_dir = os.environ.get("HRMS_DATA_DIR", ".hrms_data")
            egress_log = egress_log or EgressLog(SqliteKeyValueStore(os.path.join(data_dir, "plugin_storage.db")))
            policy_resolver = policy_resolver or env_policy_resolver()
        self.egress_log = egress_log
        self.policy_resolver = policy_resolver

    def policy_for(self, tenant_id: str) -> Any:
        from hrms_plugin.security.tenant_registry import LlmPolicy

        if self.policy_resolver is None:
            # Fail closed (agy G-F8): without a resolver only the in-process fake provider may run (tests).
            if self.config.provider is LLMProvider.FAKE:
                return LlmPolicy(allowed_providers=("fake",))
            from hrms_plugin.governance.llm_policy import LlmNotAllowed

            raise LlmNotAllowed(f"no LLM policy resolver configured for provider {self.config.provider.value!r}")
        return self.policy_resolver(tenant_id, self.config.provider.value)

    _TOKEN_KIND = re.compile(r"\[PII_([A-Z_]+?)_\d+_[0-9a-f]{8}\]")

    def _record_egress(self, tenant_id: str, protected: List[ChatMessage]) -> Optional[str]:
        """Log what is about to leave: hash of the redacted payload and the kinds redacted IN THIS CALL."""
        if self.egress_log is None:
            return None
        payload = json.dumps(
            [[m.role, m.content, [[tc.name, tc.arguments] for tc in (m.tool_calls or [])]] for m in protected],
            default=str,
            sort_keys=True,
        )
        distinct_tokens = {m.group(0): m.group(1) for m in self._TOKEN_KIND.finditer(payload)}
        kinds: Dict[str, int] = {}
        for kind in distinct_tokens.values():
            kinds[kind] = kinds.get(kind, 0) + 1
        return self.egress_log.record(
            tenant_id=tenant_id,
            provider=self.config.provider.value,
            model=self.config.model,
            payload_sha256="sha256:" + hashlib.sha256(payload.encode()).hexdigest(),
            redacted_kinds=kinds,
        )

    def _finish_egress(self, key: Optional[str], **fields: Any) -> None:
        if key is not None and self.egress_log is not None:
            self.egress_log.update(key, **fields)

    @staticmethod
    def _redact_obj(obj: Any, pii: PiiSession) -> Any:
        if isinstance(obj, str):
            return pii.redact_text(obj)
        if isinstance(obj, dict):
            return {k: LLMGateway._redact_obj(v, pii) for k, v in obj.items()}
        if isinstance(obj, list):
            return [LLMGateway._redact_obj(v, pii) for v in obj]
        return obj

    @staticmethod
    def _protect(messages: List[ChatMessage], pii: PiiSession) -> List[ChatMessage]:
        """Redact every outgoing message, then refuse to send if anything identifiable is still present."""
        protected: List[ChatMessage] = []
        leaked: List[str] = []
        for m in messages:
            content = pii.redact_text(m.content) if m.content else m.content
            tool_calls = (
                [
                    ToolCall(id=tc.id, name=tc.name, arguments=LLMGateway._redact_obj(tc.arguments, pii))
                    for tc in m.tool_calls
                ]
                if m.tool_calls
                else m.tool_calls
            )
            leaked += pii.find_pii(content or "")
            for tc in tool_calls or []:
                leaked += pii.find_pii(json.dumps(tc.arguments, default=str))
            protected.append(
                ChatMessage(
                    role=m.role,
                    content=content,
                    tool_calls=tool_calls,
                    tool_call_id=m.tool_call_id,
                    name=m.name,
                    raw_content=m.raw_content,  # provider-produced from redacted input; never altered (E2)
                )
            )
        if leaked:
            raise PiiEgressBlocked(leaked)
        return protected

    def _resolve_config_from_env(self) -> LLMConfig:
        """Autodetect active LLM provider from environment variables."""
        provider_env = os.getenv("LLM_PROVIDER", "").lower()
        known = {"", "fake", "openai", "groq", "openrouter", "anthropic", "gemini", "google", "nvidia"}
        if provider_env not in known:
            raise LLMNotConfigured(f"unknown LLM_PROVIDER {provider_env!r}")
        if provider_env == "fake":
            return LLMConfig(provider=LLMProvider.FAKE, model="fake-agentic-v1")

        if provider_env == "openai" or (not provider_env and os.getenv("OPENAI_API_KEY")):
            return LLMConfig(
                provider=LLMProvider.OPENAI,
                model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
                api_key=os.getenv("OPENAI_API_KEY"),
                base_url=os.getenv("OPENAI_BASE_URL"),
            )
        elif provider_env == "groq" or (not provider_env and os.getenv("GROQ_API_KEY")):
            return LLMConfig(
                provider=LLMProvider.GROQ,
                model=os.getenv("LLM_MODEL", "llama-3.3-70b-versatile"),
                api_key=os.getenv("GROQ_API_KEY"),
            )
        elif provider_env == "openrouter" or (not provider_env and os.getenv("OPENROUTER_API_KEY")):
            return LLMConfig(
                provider=LLMProvider.OPENROUTER,
                model=os.getenv("LLM_MODEL", "meta-llama/llama-3.3-70b-instruct:free"),
                api_key=os.getenv("OPENROUTER_API_KEY"),
            )
        elif provider_env == "anthropic" or (not provider_env and os.getenv("ANTHROPIC_API_KEY")):
            effort = os.getenv("LLM_EFFORT", "medium")
            if effort not in EFFORT_LEVELS:
                raise LLMNotConfigured(f"LLM_EFFORT must be one of {EFFORT_LEVELS}, got {effort!r}")
            return LLMConfig(
                provider=LLMProvider.ANTHROPIC,
                model=os.getenv("LLM_MODEL", ANTHROPIC_DEFAULT_MODEL),
                api_key=os.getenv("ANTHROPIC_API_KEY"),
                max_tokens=ANTHROPIC_MAX_TOKENS,
                effort=effort,
            )
        elif provider_env in ("gemini", "google") or (
            not provider_env and (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"))
        ):
            return LLMConfig(
                provider=LLMProvider.GEMINI,
                model=os.getenv("LLM_MODEL") or os.getenv("GOOGLE_MODEL") or "gemini-2.5-flash",
                api_key=os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"),
            )
        elif provider_env == "nvidia" or (not provider_env and os.getenv("NVIDIA_API_KEY")):
            return LLMConfig(
                provider=LLMProvider.NVIDIA,
                model=os.getenv("LLM_MODEL", "meta/llama-3.1-70b-instruct"),
                api_key=os.getenv("NVIDIA_API_KEY"),
            )
        raise LLMNotConfigured(
            "no LLM provider configured: set LLM_PROVIDER (or a provider API key); use LLM_PROVIDER=fake only for tests"
        )

    def _create_client(self, config: LLMConfig) -> BaseLLMClient:
        if config.provider in (LLMProvider.OPENAI, LLMProvider.OPENROUTER, LLMProvider.GROQ, LLMProvider.NVIDIA):
            return OpenAICompatibleClient(config)
        elif config.provider == LLMProvider.ANTHROPIC:
            return AnthropicClient(config)
        elif config.provider == LLMProvider.GEMINI:
            return GeminiClient(config)
        else:
            return FakeLLMClient(config)

    async def aclose(self) -> None:
        await self.client.aclose()

    async def chat(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[ToolDefinition]] = None,
        task_class: TaskClass = TaskClass.AGENT_REACT,
        temperature: Optional[float] = None,
        tenant_id: str = "DEFAULT",
        pii: Optional[PiiSession] = None,
    ) -> LLMResponse:
        """Execute a chat turn. PII is always redacted before sending and restored in the reply (piece C)."""
        policy = self.policy_for(tenant_id)  # raises LlmNotAllowed before anything is sent (piece G2)
        pii = pii or PiiSession(tokenize_compensation=policy.tokenize_compensation)
        # Pin deterministic temperature for high-stakes tasks
        effective_temp = 0.0 if task_class in (TaskClass.SCORING, TaskClass.EVALUATION) else temperature
        protected = self._protect(messages, pii)
        egress_key = self._record_egress(tenant_id, protected)
        try:
            response = await self.client.chat(
                messages=protected,
                tools=tools,
                temperature=effective_temp,
            )
        except BaseException:
            self._finish_egress(egress_key, status="FAILED")
            raise
        usage = response.usage or {}
        self._finish_egress(
            egress_key,
            status="COMPLETED",
            input_tokens=usage.get("input_tokens", usage.get("prompt_tokens")),
            output_tokens=usage.get("output_tokens", usage.get("completion_tokens")),
        )
        response.content = pii.restore(response.content)
        response.tool_calls = [
            ToolCall(id=tc.id, name=tc.name, arguments=pii.restore_obj(tc.arguments)) for tc in response.tool_calls
        ]
        return response

    async def chat_stream(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[ToolDefinition]] = None,
        tenant_id: str = "DEFAULT",
        pii: Optional[PiiSession] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Stream SSE events; PII is redacted before sending and tokens are restored in streamed text."""
        policy = self.policy_for(tenant_id)
        pii = pii or PiiSession(tokenize_compensation=policy.tokenize_compensation)
        protected = self._protect(messages, pii)
        egress_key = self._record_egress(tenant_id, protected)  # stream token usage is not reported → stays None
        restorer = StreamRestorer(pii)
        async for chunk in self.client.chat_stream(protected, tools):
            if chunk.get("event") == "token":
                text = restorer.feed(str(chunk.get("data", "")))
                if text:
                    yield {**chunk, "data": text}
                continue
            pending = restorer.flush()
            if pending:
                yield {"event": "token", "data": pending}
            yield {**chunk, "data": pii.restore_obj(chunk.get("data"))}
        pending = restorer.flush()
        if pending:
            yield {"event": "token", "data": pending}
        self._finish_egress(egress_key, status="COMPLETED")

    async def complete(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        task_class: TaskClass = TaskClass.AGENT_REACT,
        tenant_id: str = "DEFAULT",
    ) -> str:
        """High-level completion helper."""
        msgs: List[ChatMessage] = []
        if system_prompt:
            msgs.append(ChatMessage(role="system", content=system_prompt))
        msgs.append(ChatMessage(role="user", content=prompt))

        resp = await self.chat(messages=msgs, task_class=task_class, tenant_id=tenant_id)
        return resp.content
