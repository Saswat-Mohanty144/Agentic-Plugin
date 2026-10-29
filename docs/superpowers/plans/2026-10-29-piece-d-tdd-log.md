# Piece D — TDD log

| Step | Red evidence | Green |
|---|---|---|
| Core exports (escapeHtml, formatMarkdown, isTrustedMessage, sanitizeContext, validateApiUrl, buildHeaders, buildRemediationBody, describePatch, normalizeJurisdiction) | `node --test` failed: exports undefined | 55/55 pass |
| XSS payloads (`<img onerror>`, `<script>`, `javascript:` link, attribute break-out, `&` in href) | failed against old innerHTML formatter | pass; assertion = no real tag with `on*=`, no `<`/`>` after allowlisted tags removed |
| Origin check | wildcard accepted | pass (exact origin, `'*'` host origin rejected) |
| Source checks: no `postMessage(..., '*')`, no `approver_`, no `X-Tenant-Id`, no `EMP-AUTO`, no `data-action=` | all present in old file | pass |
| Orchestrator `test_remediation_actions_identify_the_record` | `KeyError: 'entity_id'` | `1 passed` |

Mutation proof (single run): remove escaping → 9 fail; remove origin check → 2 fail; loosen link regex → 1 fail.
Full suite: `ruff check .` clean; `pytest` 1010 passed; `node --test sdk/test/copilot.test.js` 55 pass.
