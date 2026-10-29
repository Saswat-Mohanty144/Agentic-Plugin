# Piece D — agy findings resolution

agy (gemini-3.8-flash-high) verdict FAIL, 9 findings (`2026-10-29-piece-d-agy-findings.json`). All reproduced and
fixed test-first; none disputed.

| ID | Sev | Fix | Red → green test |
|---|---|---|---|
| F1 | HIGH | supervisor (and orchestrator) REMEDIATE_PATCH actions carry `entity_id` + `entity_type` | `test_non_agentic_compliance_actions_identify_the_record` (KeyError 'entity_id'); orchestrator test (KeyError 'entity_type') |
| F2 | HIGH | SDK proposes via `/v1/writeback/proposals` (two-person approval server-side); `/v1/remediate/execute` reports `ATTRIBUTION_ONLY`, `applied: false` | node source test (no `/v1/remediate/execute`); `test_remediation_execute_endpoint`, e2e (was `APPROVED_AND_QUEUED`) |
| F3 | MED | composed text rendered via `textContent` (`appendPlain`); `describePatch`/`confirmationText` plain | `confirmation text is plain …` |
| F4 | MED | link URL excludes `⟦⟧` placeholders | `inline code inside a link URL cannot reach the href` |
| F5 | MED | `res.ok` checked on every fetch; `describeHttpError` | `describeHttpError *`, `every fetch response is checked` |
| F6 | LOW | entity type → doctype allowlist (`EMPLOYEE` only); others not offered | `proposal refused: unsupported entity_type` |
| F7 | LOW | patch must be a non-empty plain object | `proposal refused: array patch` (+ empty, Date) |
| F8 | LOW | XSS tests: non-link vectors assert no `<a` at all; every href checked against a strict pattern | 13 vectors |
| F9 | LOW | `&amp;` allowed inside link URLs (other entities still refused) | `https links with query parameters keep working` |

Own test-design error found while fixing: two vectors (`&lt;b&gt;`, `&#106;`) were wrongly put in the "no link" set;
their literal entity text is re-escaped (`&amp;lt;`) and inert, so they moved to the link-allowed set (href check
still applies). Extra defence: proposal `entity_id` rejects `.`/`..` path segments (the server also percent-encodes).

Mutation proof (single run): placeholder allowed in URL → 2 fail; no `&amp;` → 1; patch shape unchecked → 3;
inherited doctype key → 2; one `res.ok` check removed → 1; `describePatch` escapes again → 1.
