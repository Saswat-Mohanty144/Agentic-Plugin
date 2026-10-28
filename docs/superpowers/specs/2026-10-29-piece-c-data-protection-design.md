# Design: Piece C — Data protection on the live LLM path

* **Date**: 2026-10-29
* **Status**: Approved under user delegation ("decide as HR domain expert and AI architect; do not wait")
* **Answers**: Q3 and Q7 (enforcement part). Governance (approval workflow, payload audit log, model choice by data sensitivity) is in piece G.
* **Rules**: strict TDD (red output recorded first, then green); positive, negative and boundary tests for every behaviour; no assumptions.

## 1. Problems (verified in code at commit d6aeeb4)

| # | Problem | Evidence |
|---|---|---|
| P1 | Two separate masking instances (app with a persistent vault, LLM gateway in memory) mask text twice and don't share tokens | `server/app.py:72,147`; `ai/llm.py:650,730` |
| P2 | The persistent vault stores raw PII in plaintext SQLite, with an unsalted SHA-256 lookup | `storage/vault_store.py:47-55` |
| P3 | Unmasking acts as an oracle: any `[VAULT_…]` string in a reply is rehydrated for the tenant, whoever created it | `security/masking.py:161-183` |
| P4 | The raw request `context` (employee records, salaries, names) goes into the system prompt | `agents/orchestrator.py:130-140` |
| P5 | Full tool results (names, IDs, amounts) are sent to the LLM as `json.dumps(output)` | `agents/orchestrator.py:239,288` |
| P6 | Detectors have no checksums: "Aadhaar" is any 12 digits and "phone" is any 10 digits; names are never caught | `security/masking.py:23-30` |
| P7 | The streaming path never unmasks, so users see tokens | `ai/llm.py:754-778` |
| P8 | Token arguments written by the LLM would reach tools as literal token strings | `orchestrator.py` passes `tc.arguments` through |

## 2. Decisions log

| # | Decision | Decided by |
|---|---|---|
| C1 | Approach A: request-scoped tokens, a single checkpoint before the LLM, and a fail-closed check on outgoing data | User |
| C2 | Field classes. **IDENTIFIER** (names, email, phone, address, date of birth, national IDs, passport, bank account / IBAN / card) never reaches the LLM raw; it is tokenised. **COMPENSATION** amounts may reach the LLM only inside records whose identifiers are tokenised (pseudonymous), because the model needs figures to explain payroll. Everything else is OPERATIONAL. | Architect, under delegation (HR rationale: salary linked to an identity is sensitive personal data under DPDP / GDPR / UAE PDPL; de-identified it is not) |
| C3 | Values from the request `context` are never shown to the LLM except the allowlisted navigation keys `entity`, `record_id`, `route`. The model sees which context keys were provided, not their values. Tools get context values by trusted server-side injection (generalised from A5: any context key matching a declared tool parameter; request `jurisdiction` and token `tenant_id` always win). | Architect, under delegation |
| C4 | `mask_pii` stays in the API for compatibility but **can no longer turn protection off** | Architect, under delegation (no client can opt out of statutory data protection) |
| C5 | Remove `PiiMaskingGateway` (`security/masking.py`) and `PiiVaultStore` (`storage/vault_store.py`) and their tests. At startup, delete the `pii_vault` and `pii_vault_lookup` namespaces from the existing store, purging plaintext PII written by earlier versions. | Architect, under delegation |
| C6 | Token format `[PII_<KIND>_<n>_<nonce>]`, where `nonce` is 8 random hex characters per request, so tokens can't be predicted or replayed across requests | Architect |
| C7 | If the outgoing-data check still finds PII after redaction, the LLM call is not made. The API returns **422** `{"error": "PII_EGRESS_BLOCKED"}`. | Architect |
| C8 | Free-text names that appear in no structured record (for example "settle Rashid's dues" with no record) can't be detected without named-entity recognition. This is accepted residual risk, documented, and goes to piece G (a local NER / Presidio evaluation). | Architect |

## 3. Components

### 3.1 `hrms_plugin/security/pii.py` (new; replaces `masking.py`)

**Detectors**: pure functions returning match spans, each with validation:

| Kind | Pattern | Validation |
|---|---|---|
| AADHAAR | 12 digits, optionally in groups of 4 separated by a space or hyphen; first digit 2–9 | Verhoeff checksum |
| PAN | `[A-Z]{3}[PCHFATBLJG][A-Z][0-9]{4}[A-Z]` | 4th character is a valid holder type |
| EMIRATES_ID | `784` + 12 digits, optional hyphens `784-YYYY-NNNNNNN-C` | Luhn |
| SSN | `\d{3}-\d{2}-\d{4}` | Area not 000, 666 or 9xx; group not 00; serial not 0000 |
| IBAN | 2 letters, 2 digits, 11–30 alphanumerics (spaces allowed in groups of 4) | mod-97 = 1 |
| CARD | 13–19 digits, optional spaces/hyphens | Luhn |
| EMAIL | standard address regex | — |
| PHONE | `+` followed by 8–15 digits (spaces/hyphens allowed), or Indian mobile `[6-9]\d{9}` | — |

**Identifier field names** (case-insensitive, after normalising to snake_case):
- Names: `name`, `first_name`, `last_name`, `full_name`, `employee_name`, `candidate_name`.
- Contact: `email`, `work_email`, `personal_email`, `phone`, `mobile`, `phone_number`, `address`.
- Dates: `dob`, `date_of_birth`.
- National IDs: `aadhaar`, `aadhaar_number`, `pan`, `pan_number`, `ssn`, `emirates_id`, `passport`, `passport_number`, `national_id`.
- Financial: `bank_account`, `account_number`, `iban`, `card_number`.

**`PiiSession`**: one per request (or per LLM call when no session is supplied):
- `learn(obj)`: walks dicts and lists; records the string value of every identifier-named field (non-empty, at least 2 characters) as a known value.
- `redact_text(text)`: replaces known values (longest first, exact substring), then detector matches, with stable tokens (the same value always gets the same token within the session).
- `restore(text)` / `restore_obj(obj)`: replace **only this session's** tokens with their originals.
- `find_pii(text)`: list of kinds still present (known values or detector hits).
- `StreamRestorer`: buffers an incomplete trailing token so a token split across stream chunks is restored correctly.

### 3.2 `LLMGateway` (single checkpoint)
- `chat(..., pii: PiiSession | None = None)` and `chat_stream(..., pii: PiiSession | None = None)`. When `pii` is `None`, a fresh session is used for that one call.
- For every message:
  1. `content = pii.redact_text(content)`.
  2. Check the result: if `pii.find_pii(content)` is not empty, raise `PiiEgressBlocked(kinds)`; the provider is not called.
- Tool-call arguments in assistant messages are redacted the same way.
- Response `content` and tool-call `arguments` are restored with `pii.restore` / `restore_obj` before being returned.
- Stream `token` events pass through `StreamRestorer`.

### 3.3 Orchestrator
- One `PiiSession` per run. Calls `learn(context)` and, after each tool call, `learn(output)`.
- The system prompt's `Context:` line becomes:
  `Context fields provided by the host application (values are passed to tools automatically): <sorted keys>; navigation: <allowlisted key=value>`.
- Trusted values are the context keys matching declared parameters, then the request `jurisdiction` and token `tenant_id` on top, so they always win.
- Before execution, tool arguments are `pii.restore_obj(arguments)`, so tokens written by the LLM become real values server-side.
- The session is passed to every gateway call.

### 3.4 Server
- Removes `pii_gateway` and `vault_store`; the request handlers do no masking (the gateway does it all).
- `PiiEgressBlocked` → 422 `{"error": "PII_EGRESS_BLOCKED", "kinds": [...]}`. The response lists only kinds, never values.
- Startup purge of legacy vault namespaces (C5).

## 4. Tests (positive, negative, boundary)

| Area | Positive | Negative | Boundary |
|---|---|---|---|
| Aadhaar | valid Verhoeff number, plain and grouped | 12 digits with a bad checksum; first digit 0 or 1 | 11 and 13 digits not matched; embedded in a longer number not matched |
| PAN | `ABCPE1234F` | invalid holder type `ABCXE1234F` | lowercase not matched |
| Emirates ID | valid Luhn 784 number, with and without hyphens | bad Luhn | prefix other than 784 |
| SSN | `123-45-6789` | `000-…`, `666-…`, `9xx-…`, `…-00-…`, `…-0000` | — |
| IBAN | `GB82 WEST 1234 5698 7654 32`, `AE070331234567890123456` | bad mod-97 | 15 and 34 characters accepted (shortest and longest real formats) |
| Card | `4111 1111 1111 1111` | bad Luhn | 12 and 20 digits not matched |
| Email / phone | `a@b.co`; `+971 50 123 4567`; `9876543210` | `5876543210` (Indian mobiles start 6–9) | `+1234567` (7 digits) not matched; 15 digits matched; 16 not matched |
| Session | same value → same token; restore round-trips; nested `learn` | a token from session A is not restored by session B (oracle gone); a guessed token string is not restored | empty strings and 1-character names are not learned; overlapping known values: longest wins |
| Stream | token split across 2 and 3 chunks restored | unknown `[PII_…]` string passed through unchanged | stream ends mid-token: the buffer is flushed verbatim |
| Gateway | the provider sees no raw Aadhaar, name or email; the reply is restored | with redaction disabled by a monkeypatch: `PiiEgressBlocked`, provider not called | message with no PII: unchanged |
| Orchestrator | the system prompt lists context keys only; tool gets the context value by injection; a name from a tool result is absent from the next LLM call; tool args containing tokens are restored before execution | context `employees` names never in any LLM call | allowlisted `record_id` shown; request `jurisdiction` beats context `jurisdiction` |
| Server | chat end to end: scripted LLM receives no PII, reply restored; stream restored | `mask_pii: false` is still protected | legacy `pii_vault*` rows purged at startup; other namespaces untouched |

## 5. Out of scope
- **Piece G:** NER for free-text names, payload audit log, model choice by data sensitivity, field-classification approval workflow.
- **Piece H:** compaction of tool results for cost.

## 6. Amendments made during implementation (architect, under delegation)

| # | Amendment | Why |
|---|---|---|
| C9 | The grouped Aadhaar format (`XXXX XXXX XXXX` / `XXXX-XXXX-XXXX`, consistent separators) is redacted **even with a bad Verhoeff checksum**. Plain 12-digit numbers still need a valid checksum. A grouped Aadhaar may not be part of a longer grouped number (for example a card). | HR domain: a mistyped Aadhaar in printed format is still personal data, while ungrouped 12-digit values are often account or reference numbers. |
| C10 | Tool-call arguments are restored inside `LLMGateway` (the single checkpoint) rather than in the orchestrator, as §3.3 originally said | One place for both redaction and restoration; the orchestrator test proves tools receive real values |
| C11 | A pre-existing crash was fixed: `json.dumps(tool_output)` raised on `Decimal` values (`evaluate_onboarding_offer`) | Found by the C3 tests; it existed at baseline 3028359 |
