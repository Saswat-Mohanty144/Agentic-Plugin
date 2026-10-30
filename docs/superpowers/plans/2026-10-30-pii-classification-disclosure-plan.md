# PII classification rules and disclosure lookup: plan and decisions (2026-10-30)

Source: user suggestion "PII classification is a simple layer on top of schema translation with a decision model,
to check whether a PII has been shared". Critic verdict and choices: rules as a YAML decision table (no new
dependency; JDM/zen-engine only if business users need its editor); open-connector and Airbyte not adopted (fit and
licence, see README section 20).

Branch `agentic-ai`. Tasks K1 and K2 run in worktrees `/Users/satosh/Agentic-Plugin-gap-k1` / `-k2` (branches
`gap/k1`, `gap/k2`), test-first: new tests fail before the change (red output saved to
`docs/superpowers/plans/2026-10-30-gap-k<n>-red.txt`) and pass after. Positive, negative and boundary cases for every
behaviour. Interpreter `/Users/satosh/Agentic-Plugin-main/.venv/bin/python`.

## Decisions

| ID | Decision |
|---|---|
| K1.1 | Rules file `hrms_plugin/governance/classification_rules.yaml`, loader/evaluator `hrms_plugin/governance/classification_rules.py`. Shape: `version: 1`, `hit_policy: first`, `rules: [{id, when, then, why}]`. `when` keys (all optional, AND-ed; at least one required): `entity_in` (list of canonical entity types, upper case), `target_in` (list of canonical field names), `target_kind_in` (subset of `IDENTIFIER`, `MONEY`; `IDENTIFIER` = `pii.identifier_kind(target)` is not None, `MONEY` = `pii.is_compensation_key(target)`). `then`: `data_class` in `DATA_CLASSES`, `llm_visible` bool. Load errors (raise at load, tested): unknown `when`/`then` key, empty `when`, duplicate `id`, unknown class, `llm_visible: true` with PII/COMPENSATION, unknown `hit_policy`. No match -> no suggestion. |
| K1.2 | Rule content for the canonical model: PII = names, emails, phones, `resume_text`, `resume_uri`, every `*_ref` / `assigned_to` (identify a person), `reason` (free text, may carry health details). COMPENSATION = `basic_salary`, `gross_salary`, `net_pay`, `total_allowances`, `total_deductions`, EXPENSE_CLAIM `amount`. PUBLIC/INTERNAL with `llm_visible: true` only for descriptive fields: `title`, `department`, `designation`, `location`, `status`, `stage`, `leave_type`, `claim_type`, `asset_type`, `headcount`, `must_have_skills`, `nice_to_have_skills`, `skills`, `currency`, `month`, `year`, `fiscal_year`, `min_years_experience`, `max_years_experience`. Everything else INTERNAL, `llm_visible: false`. Then generic fallbacks: `target_kind_in: [IDENTIFIER]` -> PII, `[MONEY]` -> COMPENSATION. |
| K1.3 | Synthesize (both routes) puts `suggested_classification: {data_class, llm_visible, rule_id, why}` (or null) on each `coverage.fields[target]`. Value cross-check when `sample_records` are sent: for each mapped field, every scalar sample value at its source is run through `pii.detect(pii.normalise(str(v)))`; any match while the suggestion is not PII (or no suggestion) -> suggestion becomes `{PII, false, rule_id "VALUE_CHECK", why "sample values look like <KINDS sorted, comma-joined>"}`. Values are never stored or returned. |
| K1.4 | Review (`.../review`) recomputes rule suggestions for the new draft; a VALUE_CHECK suggestion is kept when that field's source is unchanged. |
| K1.5 | Approve body gains `accept_suggested_classification: bool = false`. When true, a field without an explicit entry takes its suggestion (`data_class`, `llm_visible`); fields with neither stay PII/hidden (unchanged default). Explicit entries always win. The version stores `classification_origin: {target: "MANUAL" | "SUGGESTED:<rule_id>" | "DEFAULT"}`. Flag false -> behaviour identical to today. |
| K2.1 | Disclosure index on the egress log (same rows, same retention, purge and deletion). Rows gain `channel` (`llm`, `mcp`, `voice`) and `subject_refs: [{ref, kind, outcome}]`, `outcome` `WITHHELD` (replaced by a token before sending) or `DISCLOSED` (sent in clear). Index namespace `llm_egress_subjects`, key `<tenant>:<ref>` -> list of egress row keys; `purge` and `delete_tenant` remove index entries for removed rows. |
| K2.2 | `ref = "hmac-sha256:" + HMAC-SHA256(key, tenant_id \x00 kind \x00 canonical(value)).hexdigest()`. `canonical`: `pii.normalise`, casefold, strip; for AADHAAR, PAN, CARD, IBAN, SSN, EMIRATES_ID, PHONE also remove spaces and hyphens. Key from file `HRMS_DISCLOSURE_KEY_FILE` (raw bytes, at least 32, else not configured). No key -> rows get `subject_refs: []` and `subject_index: false`; nothing else changes. A plain hash is never stored (low-entropy ids are guessable). |
| K2.3 | LLM: refs come from the tokens in the protected payload of that call (their original values, from the PII session through a public method), outcome WITHHELD. MCP `tools/call`: after a successful call, walk the JSON output: string leaves through `pii.detect`; scalar values under keys with `pii.identifier_kind(key)` not None (kind from the key) ; outcome DISCLOSED; added via `EgressLog.update`. Voice: STT row `redacted_kinds {"AUDIO": 1}` is not added (audio cannot be inspected): row gets `channel: voice`, `subject_refs: []`; TTS text is detected like MCP (normally empty, since PII text is withheld). |
| K2.4 | `POST /v1/governance/disclosures/lookup`, body `{kind, value}` (POST so the value is never in a URL or access log; `kind` one of the detector/identifier kinds, `value` 1-256 chars). Needs `MANAGE_GOVERNANCE` with recent sign-in, rate-limited. `503 DISCLOSURE_INDEX_NOT_CONFIGURED` without key. Response `{kind, found, disclosures: [{at, channel, provider, model, outcome, status}]}`, newest first, at most 200, caller's tenant only, never echoes the value. Every lookup is recorded in namespace `disclosure_lookups` (`tenant_id`, `user_id`, `at`, `kind`, `found`; no value, no ref). |

## Tasks

| Task | Decisions | Main files |
|---|---|---|
| K1 | K1.1-K1.5 | new `governance/classification_rules.py` + `.yaml`, `schema/synthesizer.py`, `storage/mapping_store.py`, `server/app.py` (approve body) |
| K2 | K2.1-K2.4 | `governance/egress_log.py`, `security/pii.py` (public accessor), `ai/llm.py`, `server/mcp_http.py`, `server/app.py` (voice, new route) |

Acceptance per task: its new tests pass, previously passing tests in touched areas pass, `ruff check hrms_plugin tests`
clean. After merging: full suite, one mutation proof, README update.

## Parked

- Labour-law and data-protection review of the rule content (K1.2) by a privacy/legal reviewer: NEEDS_HUMAN. The
  rules are conservative defaults and every suggestion needs a person to accept it.
- Airbyte manifest importer: after a licence check per connector (NEEDS_HUMAN: legal).

## Audit round (2026-10-30, agy adversarial audit, verified by the orchestrator)

Report: `docs/superpowers/plans/2026-10-30-audit-k-report.md` (see its "Orchestrator verification" section).

| ID | Finding (verified) | Decision |
|---|---|---|
| A1 | BUG-1: introspect drift never reports missing expected fields although README section 12 promises it | Introspect calls the drift check with `check_missing=True` against the ACTIVE mapping; a mapped single-source field absent from every sample record -> WARNING event `ADD_REQUIRED_FIELD` (field name only) |
| A2 | BUG-2: `overlay()` always rebuilds a REST `EntitySpec`, so an MCP profile with an approved mapping fails | Rebuild with the spec's own class (`EntitySpec` or `McpEntitySpec`); protocol keys preserved |
| A3 | BUG-3: `discovered_fields` item without a string `name` -> 500 | `422 INVALID_ARGUMENT` naming the item index; no 500 |
| A4 | BUG-4: two routes raise bare `HTTPException` (`{"detail": ...}` without `error`) | Introspect with neither swagger nor samples -> `422 INVALID_ARGUMENT`; unknown mapping -> `404 NOT_FOUND`; no `HTTPException` left in `server/` |
| A5 | BUG-5: deploy files do not pass `HRMS_DISCLOSURE_KEY_FILE` | Compose passes it through; k8s mounts optional Secret `hrms-plugin-disclosure-key` read-only at `/etc/hrms/disclosure` and sets the variable to `/etc/hrms/disclosure/disclosure.key` |
| A6 | Q2: re-approving the shape / payroll fingerprint that is already approved opens a new two-person request | Return `200 {"status": "ALREADY_APPROVED", "fingerprint": ...}` and create no pending request; a different fingerprint keeps the two-step flow |
| A7 | Q1: README "others get a spec built on the fly" is ambiguous | Doc only: the four supported types without a canonical spec (interview, offer, payroll run, appraisal) get one built on the fly; any other domain -> `422 INVALID_DOMAIN` |
| A8 | Q3: every MCP frame (including `ping` and notifications) is rate-limited | Keep (protects against floods); document it |
| A9 | BUG-6 (widget drops `thinking`, `history_trimmed`) | Not a defect: the widget renders an allow-list; `thinking` carries raw model reasoning and stays out of the browser. The audit test is corrected to pin the allow-list |
| A10 | Audit test `test_requisition_shape_approval_guards` leaves a malformed approved shape in the shared store, breaking 3 existing tests | Audit tests must restore every global they touch; full suite with audit tests first must show 0 failures |
