# Stakeholder voice-note recommendations (2026-10-30): plan and decisions

Source: two voice notes (transcribed locally with whisper.cpp; audio not uploaded anywhere).

## Recommendations, alignment, critic's verdict

| # | Recommendation | Code today | Verdict |
|---|---|---|---|
| V1 | Schema adaptation is partly a product-expectation problem; not every mismatch needs fixing | Fail-closed design, no guessed values | Valid; document the promise (README) |
| V2 | Never promise 100% match; show what maps today | Synthesis report lists confidence and unmapped fields, but only in the HTTP response; the stored version keeps only the mapping | Valid; store a coverage report with each version |
| V3 | Map what exists; show the rest as missing | Unmapped lists exist, no per-field status | Valid; per-field status MATCHED / NEEDS_REVIEW / MISSING / UNUSED |
| V4 | Draft + expert accepts/rejects, manual configuration fallback | PROPOSED + four-eyes approve of the whole version; no reject, no per-field decision, no manual edit | Valid, highest credibility gain |
| V5 | Keep connectivity protocols open (REST, MCP, other) | Declarative profiles are REST-only | Valid as an explicit seam; MCP adapter generic and tested against a fake server only |

## Decisions (architect, under standing delegation)

- **Coverage report (V2, V3).** Every proposed version stores `coverage`: per canonical field `status` MATCHED (confidence >= 0.8, rule origin), NEEDS_REVIEW (confidence < 0.8 or origin LLM), MISSING (no source); `source`, `confidence`, `origin` (RULE / LLM / MANUAL), `rationale`, `required`; plus `unused_host_fields` and counts. Returned by synthesize and by `GET /v1/schema/mappings` (version rows).
- **Review actions (V4).** `POST /v1/schema/mappings/{vendor}/{entity_type}/versions/{version}/review` (MANAGE_SCHEMA, recent sign-in) with `accept: [targets]`, `reject: [targets]`, `manual: {target: {source, transform?, values?}}`. It creates a new PROPOSED version (decisions applied, rejected fields removed and shown MISSING, manual fields origin MANUAL and status MATCHED, accepted fields status MATCHED with `accepted_by`), marks the reviewed version `REVISED`, and records the decisions. Unknown targets or invalid transforms are refused (422).
- `POST .../versions/{version}/reject` with a `reason` marks a PROPOSED version `REJECTED`.
- **Approval gate.** Approving a version that still has NEEDS_REVIEW fields fails with `409 REVIEW_REQUIRED` listing them. Four-eyes stays: the approver must differ from the proposer of that version (the reviewer who created a revision is its proposer).
- **Protocol seam (V5).** Vendor profiles take `protocol: rest | mcp` (default `rest`; anything else refused at load). `mcp` profiles name a `tool` and optional `arguments`, `items_at`, cursor paging (`cursor_param`, `next_cursor_at`); the connector speaks MCP Streamable HTTP JSON-RPC (`initialize`, `notifications/initialized`, `tools/call`, honouring `Mcp-Session-Id`) with bearer auth, and maps results through the same field rules, shape checks and rejections as REST. Not proven against any real vendor MCP server.
