# Token waste — root fixes (2026-10-29)

Evidence audit (read-only probe + tests, commit 7fcad54): fixed = #1 no-tool stream (1 call), #6 one assistant
message per turn. Partly fixed = #2, #3, #8. Not fixed = #4, #5, #7, #9. Owner decision: oversize history is trimmed
(oldest first, reported); an oversize message is refused.

| # | Design | Evidence required |
|---|---|---|
| 7 | `message` <= 8,000 chars on /v1/chat, /v1/chat/stream, legacy chat, voice text -> 422 `MESSAGE_TOO_LONG`. History total <= 24,000 chars: oldest user/assistant pairs dropped until it fits, history still starts with a user turn, response carries `history_trimmed: n`. | Boundary tests at 8,000 / 8,001; trimmed history sent to the LLM is within budget and starts with a user turn |
| 5 | If the last allowed step still asks for tools, one more call with no tools offered forces an answer from the gathered results; reply `partial: true`. "Completed N steps" never returned. | Scripted model: last call has no tools; reply is the model's text |
| 4 | Tools may declare a model view: archetypes -> id/name/badge; compliance audit -> violation ids, summaries, citations. UI keeps the full output (`structured_data`, stream events). Others unchanged. | Characters sent to the model per tool before/after; UI still gets full output |
| 9 | `max_tokens` per task: CLASSIFICATION/SCORING/EVALUATION 512, EXTRACTION 1,024, AGENT_REACT/POLICY_QA 2,048, SYNTHESIS/DRAFTING 4,096. Length-stopped replies are marked `truncated: true`. | Each provider client receives the task's cap |
| 2 | One app-level gateway is passed to the synthesizer (no gateway built per request); upgraded mapping keeps `version_path` and `collection_path`. | Route builds no gateway; fields preserved |
| 3 | Add single-word intent keywords (gratuity, leave, salary, pf, payslip, resume, expense); fallback to all permitted tools stays. | Tools offered per phrase; schema chars per intent |
| 8 | Prefix identical across tenants/contexts (test); OpenAI `prompt_tokens_details.cached_tokens` recorded as cache-read tokens. | Usage mapping test; prefix equality test |
