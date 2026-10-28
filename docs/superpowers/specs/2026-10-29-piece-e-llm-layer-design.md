# Design: Piece E — LLM layer correctness

* **Date**: 2026-10-29
* **Status**: Decided by architect under user delegation
* **Answers**: Q4 (part: the wasted LLM mapping spend) plus the LLM-layer defects from the first audit
* **Rules**: strict TDD (red output recorded first); positive, negative and boundary tests; no assumptions. Anthropic usage follows the bundled `claude-api` skill reference (cached 2026-10-06), not memory.

## 1. Problems (verified at commit 3d966bf)

| # | Problem | Evidence |
|---|---|---|
| E-P1 | The Anthropic client drops `tool_use` blocks from assistant turns and sends `tool_result`s referencing ids that don't exist. It sends `temperature`, which returns a 400 on every model since 2026-10-21. It defaults to the retired `claude-3-5-sonnet-20241022`, and it hand-rolls HTTP instead of using the official SDK. | `ai/llm.py` AnthropicClient |
| E-P2 | The orchestrator appends one assistant message per tool call, so parallel calls are split across turns and Anthropic requires them in one | `agents/orchestrator.py` run/run_stream |
| E-P3 | `run_stream` calls the LLM twice when no tool is used | `orchestrator.py` run_stream |
| E-P4 | `synthesize_agentic` builds `FieldMap(canonical=…, host=…)`, which raises a `TypeError` that is swallowed, so every LLM mapping is paid for and discarded | `schema/synthesizer.py:832,860` |
| E-P5 | Without provider configuration the service silently uses `FakeLLMClient` in production | `ai/llm.py` `_resolve_config_from_env` |
| E-P6 | No retries on 429/5xx; a new HTTP client per request (TLS handshake every call); Gemini API key sent in the URL query string | `ai/llm.py` |
| E-P7 | Client-supplied `history` roles are trusted (a `system` turn can override the operator prompt); no size bounds | `server/app.py` ChatRequest, `orchestrator.py` |

## 2. Decisions (architect, under delegation)

| # | Decision |
|---|---|
| E1 | The Anthropic provider uses the **official `anthropic` SDK** (`AsyncAnthropic`, pinned `anthropic>=1.17,<2`) through `client.beta.messages.create`. Default model **`claude-opus-5-5`** (overridable via `LLM_MODEL`). `max_tokens=16000`. The `thinking` parameter is omitted (adaptive, always on for this model). `output_config={"effort": LLM_EFFORT}`, where `LLM_EFFORT` is one of `low` / `medium` / `high` / `xhigh` / `max`, default `medium`, and any other value raises `LLMNotConfigured`. No `temperature`. Server-side refusal fallback enabled by default (`betas=["server-side-fallback-2026-07-01"]`, `fallbacks="default"`), per the reference's default. SDK retries: `max_retries=2`. |
| E2 | `LLMResponse` and `ChatMessage` gain `raw_content`: the provider-native assistant content. The orchestrator appends **one assistant message per LLM turn** carrying all `tool_calls` and `raw_content`, followed by the tool results. The Anthropic converter replays `raw_content` **unchanged**, which preserves thinking blocks. Assistant messages without `raw_content` become `text` + `tool_use` blocks. Consecutive `tool` messages become **one** user message of `tool_result` blocks. The PII checkpoint does not modify `raw_content`: it was produced by the provider from already-redacted input, and altering signed thinking blocks would invalidate them. |
| E3 | `stop_reason == "refusal"` → `finish_reason="refusal"`, with content `"This request was declined by the model provider's safety system."`; no tool calls are executed. The orchestrator returns that as the reply. |
| E4 | `run_stream` never calls the LLM a second time when the first response has no tool calls; it streams that response's text as `token` events. |
| E5 | The synthesizer uses `FieldMap(target=…, source=…)`. LLM mapping failures are **logged** with a reason (`LLM_MAPPING_FAILED`) instead of being swallowed silently; the rule-based mapping is still returned. |
| E6 | If no provider is configured and `LLM_PROVIDER` is not explicitly `fake`, or `LLM_PROVIDER` is unknown, creating `LLMGateway` raises `LLMNotConfigured`. The API maps it to **503** `{"error": "LLM_NOT_CONFIGURED"}`. Routes that don't need the LLM are unaffected. |
| E7 | OpenAI-compatible and Gemini clients each reuse **one** `httpx.AsyncClient`. They retry on 429/500/502/503/504/529, at most 2 retries, with exponential backoff `0.5·2ⁿ` s that honours `Retry-After`, capped at 30 s. The sleep function is injectable for tests. The Gemini key moves to the `x-goog-api-key` header. |
| E8 | `ChatRequest.history`: each item `{"role": "user" or "assistant", "content": str of at most 32,000 chars}`, at most 50 items; anything else → 422. |

## 3. Tests (positive, negative, boundary)

| Area | Positive | Negative | Boundary |
|---|---|---|---|
| E1 Anthropic request | body has model `claude-opus-5-5`, `max_tokens` 16000, `output_config.effort`, tools in Anthropic schema, `system`, `fallbacks:"default"`, beta header (checked on the wire via a mock transport) | body has **no** `temperature`; invalid `LLM_EFFORT` → `LLMNotConfigured` | `LLM_MODEL` override honoured; each of the 5 efforts accepted |
| E2 replay | `tool_use` id in the assistant turn matches the following `tool_result`; thinking block replayed byte-identical; 2 parallel calls → 1 assistant message + 1 user message with 2 results | assistant without `raw_content` builds `tool_use` blocks from `tool_calls` | empty assistant text not sent as an empty text block |
| E3 refusal | `stop_reason:"refusal"` → `finish_reason` refusal, no tool executed, safe reply | — | refusal on the second turn after a tool ran |
| E4 stream | no-tool stream → exactly 1 LLM call, tokens equal the text | — | empty text → no token events, still ends with an `intent` event |
| E5 synthesizer | the LLM match is applied to the mapping and the report | invalid LLM JSON → logged, rule mapping returned | LLM proposes an unknown host field → ignored |
| E6 fail loud | `LLM_PROVIDER=fake` works | nothing configured → `LLMNotConfigured`; HTTP chat → 503 | unknown `LLM_PROVIDER` → `LLMNotConfigured` |
| E7 retries | 429 then 200 → success after 1 retry; `Retry-After: 3` honoured | 400 not retried; 3 × 503 → error after 2 retries | `Retry-After: 99` capped at 30; one client reused across calls; Gemini key in header, not URL |
| E8 history | user/assistant accepted | `system` or `tool` role → 422; non-string content → 422 | 50 items OK, 51 → 422; 32,000 chars OK, 32,001 → 422 |
