# ADR 0001 — Build or buy the host-HRMS connector layer

- Status: **Proposed** (decision is NEEDS_HUMAN: product owner + finance + legal/DPA)
- Date: 2026-10-29
- Context sources: docs/PHASE_WISE_PLAN.md Q5 and Phase 11; piece I spec
  (docs/superpowers/specs/2026-10-29-piece-i-second-vendor-design.md)

## Context
The plugin must read (and, for Frappe, write back with two-person approval) records in each customer's HRMS. Two
vendors now run through one engine with configuration only: Frappe (payroll PF, write-back, requisitions) and iCeipts
(requisitions), with zero vendor-specific code hooks. Each further vendor costs: verifying its API from primary
docs, a YAML profile, contract tests, and for payroll a verified payslip mapping plus schema-fingerprint approval.

Market options named in Q5: commercial unified HRIS APIs (Merge, Finch, Kombo, Knit, Apideck, Unified.to) and open
source integration infrastructure (Nango; Airbyte/Singer are read-only sync).

## Options
1. **Build** — keep the declarative engine; add vendors as profiles.
2. **Buy (pass-through unified API)** — one unified model; vendor requests proxied, data not stored by the provider.
3. **Buy (sync/store unified API)** — provider stores copies of customer HR data.
4. **Hybrid** — unified API (or Nango) for long-tail read-only vendors; own engine for payroll-grade and write paths.

## Decision criteria (weights to be set by the owner)
| Criterion | Why it matters here | Hard requirement? |
|---|---|---|
| Data residency / sub-processor | Payroll and identity data of Indian/GCC employees; DPA per customer | Yes — option 3 likely fails |
| Payroll field depth | PF needs earnings rows incl. statistical flags; unified models often expose gross/net only | Yes for PF vendors |
| Write-back semantics | Optimistic concurrency on `modified`, rollback, audit headers (piece F2) | Yes for write vendors |
| Schema-drift control | Fingerprint + quarantine before calculation (piece F1) must still be possible | Yes |
| Coverage of target customers' HRMS | Count of customers unblocked per option | No |
| Cost per connected tenant | Per-connection pricing vs engineering time per profile | No |
| Lock-in / exit | Re-onboarding effort if the provider is dropped | No |

## Evaluation protocol (time-boxed, Phase 11)
- One pass-through unified API and Nango, each against the same test tenant (the Frappe sandbox).
- Measure, per option: can it return Salary Slip earnings rows with the fields our fingerprint uses? Can it update a
  record with an `If-Unmodified`-style guard or equivalent? Where is data stored and processed? Time to onboard
  iCeipts (custom vendor) — does the option support custom vendors at all?
- Record evidence (requests, responses with PII removed) in docs/adr/evidence/0001/.

## Preliminary architect recommendation (non-binding)
Option 4 (hybrid): keep the own engine for payroll-grade reads and every write (hard requirements above), and evaluate
a pass-through provider only for read-only long-tail entities. Rationale: the hard requirements are exactly what
unified models tend to flatten; the own engine already onboards a structurally different vendor with zero code.

## Consequences
- If Build/Hybrid: each new payroll vendor still needs a verified API spec from its owner (currently missing for the
  iCeipts payroll API and for Zoho Payroll / Keka, whose payslip APIs could not be verified from public docs).
- If Buy: the provider becomes a sub-processor in every customer DPA; the LLM egress log and PII controls are
  unaffected (the provider never sees LLM traffic).

## NEEDS_HUMAN
- Owner decision and weights; evaluation accounts/budget; legal review of the provider DPA.
