# Piece D — SDK security (embeddable `<hrms-copilot>`)

Status: decided by architect under user delegation (2026-10-29). Base: 3b66e09 (piece G).

## Threat model (adversarial audit of sdk/copilot.js before the change)
| # | Finding in old SDK | Risk |
|---|---|---|
| T1 | LLM/markdown output inserted via `innerHTML` without escaping | Stored/reflected XSS from model output or HRMS data (prompt-injected record text) |
| T2 | `message` listener accepted any origin; host posted with `'*'` | Any frame/window could steer context; context leaked to any origin |
| T3 | `X-Tenant-Id` header + `data-tenant` attribute chose the tenant | Tenant chosen by the browser, not by the signed token (cross-tenant) |
| T4 | `approver_*` fields and `EMP-AUTO` placeholder sent on remediation | Client self-approval; writes against a fabricated employee id |
| T5 | `data-action` attributes carried JSON executed on click | DOM-injected actions; no confirmation before a write |
| T6 | No URL validation for `data-api-url` | Token sent over http or to a userinfo-smuggled host |

## Decisions
- D1 Escape first: `formatMarkdown` escapes all HTML, then re-adds a fixed allowlist (`strong`, `em`, `code`, `br`, https links whose URL holds no entity except `&amp;` and no code placeholder (agy D-F4/F9), `rel="noopener noreferrer"`). Citations and labels use `textContent`.
- D2 `postMessage`: accept only `event.origin === data-host-origin` (exact, never `'*'`) and `type === 'HRMS_NAVIGATE'`; `sanitizeContext` keeps only `entity` (`ENTITY_RE`) and `record_id` (`RECORD_ID_RE`). Outbound archetype messages go to `hostOrigin` only. No host origin → messages ignored.
- D3 Auth: `tokenProvider` is a JS property (never an attribute) returning a host-IdP token; `Authorization: Bearer` only. Tenant comes from the verified token (piece A+B). Empty/whitespace token → no request.
- D4 Remediation (amended after agy D-F2): the SDK never approves or executes. `buildProposalBody` turns a
  `REMEDIATE_PATCH` action (needs `entity_type`, `entity_id`, plain non-empty `patch`) into a write-back proposal
  `{doctype, name, patch}` for supported types only (`EMPLOYEE → Employee`); the user confirms plain-text
  `confirmationText`, then the SDK POSTs `/v1/writeback/proposals`. Approval is a different person with a fresh token
  (piece F2), and the host `writable_fields` allowlist decides which fields may change. Supervisor and orchestrator
  both emit `entity_id` and `entity_type`. `/v1/remediate/execute` writes nothing and now says so
  (`status: ATTRIBUTION_ONLY`, `applied: false`).
- D7 (agy D-F3/F5): text the SDK composes is rendered with `textContent` (`appendPlain`); every `fetch` response is
  checked with `res.ok` and failures are shown via `describeHttpError`.
- D5 `validateApiUrl`: https, or http only for `localhost`/`127.0.0.1`; userinfo rejected.
- D6 Testability: pure core exported via `module.exports` and tested with `node --test` (no npm deps); CI job `sdk-tests`.

## Out of scope / NEEDS_HUMAN
- CSP header values for host pages (host owns them).
- Host token endpoint (`/session/plugin-token` in demo is illustrative only).
