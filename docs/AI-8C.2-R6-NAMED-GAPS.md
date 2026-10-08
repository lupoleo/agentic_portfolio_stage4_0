# AI-8C.2-R6 — PARTIAL Research Must Name What Is Missing

| Field | Value |
| --- | --- |
| Type | Revision of a reopened contract (AI-8C.2 research validator and repair) |
| Branch | `ai-8c2-r6-named-gaps` |
| Status | IMPLEMENTED — LIVE ACCEPTANCE PENDING |
| Approved by | Operator, 2026-10-08 (targeted repair only; software never promotes) |

## 1. Reason

In the live session of 2026-10-08 both A2A.MI research were `PARTIAL` with
HIGH evidence quality and nothing open: no unknown, no material or context
gap and no forward uncertainty.

- `NEW_LONG` also declared `requires_additional_research=false`. The semantic
  error `PARTIAL_WITHOUT_MORE_RESEARCH` triggered the repair, the model kept
  the same incoherent answer, and the hypothesis ended `PROCESSING_FAILED`
  (a wasted LLM budget slot and a candidate for retry and quarantine).
- `NEW_SHORT` declared that more research was needed without saying what. It
  was accepted as `PARTIAL` and excluded as `RESEARCH_NOT_COMPLETE`.

Neither was eligible for the AI-8C.2-R5 reassessment, which requires at
least one future outcome.

## 2. Change

1. **New warning `PARTIAL_WITHOUT_NAMED_GAP`.** A `PARTIAL` research with
   MEDIUM or HIGH evidence quality, no material gap after all rules and no
   future outcome (so not covered by R5) raises it. It triggers one
   field-scoped repair of `unknowns`, `research_status` and
   `requires_additional_research` with the instruction "PARTIAL STATUS
   REASSESSMENT": name the specific as-of fact that is missing and keep
   `PARTIAL`, or set `COMPLETE` if no such fact is missing; do not invent
   facts and do not list future outcomes as unknowns. The model decides.
2. **Deterministic normalisation, Case C.** If after the repair a `PARTIAL`
   research is otherwise valid and its only error is
   `PARTIAL_WITHOUT_MORE_RESEARCH`, `requires_additional_research` is set to
   `true` and the research stays `PARTIAL`, instead of failing. This is the
   conservative direction, like the existing cases A and B.
3. Metadata `named_gap_reassessment_requested`. Contract
   `ai-8c2-research-v5-named-gaps`; the research prompt is unchanged.
4. The validation harness reports `initial_research_status`,
   `forward_reclassification_requested`, `named_gap_reassessment_requested`
   and `deterministic_status_normalized` for every research.

Unchanged: material terms, missing-data and forward markers, status and
quality rules, scoring, thresholds (60 / 0.40). A `COMPLETE` produced by the
reassessment is validated again and demoted if it contains a material gap.
Software never promotes research to `COMPLETE`.

## 3. Offline estimate

On the 11 research of 2026-10-08: both A2A.MI research receive the
reassessment; the other nine are unaffected (`COMPLETE`, or `PARTIAL` with a
material gap). The `NEW_LONG` failure becomes at worst a `PARTIAL` research.

## 4. Residual risk

One more LLM call for eligible research. The model could promote a research
whose missing fact it fails to name; the unchanged score gate (adjusted
score ≥ 60, score confidence ≥ 0.40) and the shadow ledger remain the
safeguards.

## 5. Tests

`tests/test_ai_research_named_gaps.py` (12 tests): warning with no unknowns
and with context gaps only; no warning with a material gap, with forward
items (R5 path), with LOW quality or with `COMPLETE`; reassessment to
`COMPLETE`; missing fact named and `PARTIAL` kept; no promotion when the
model keeps `PARTIAL`; the A2A.MI `NEW_LONG` case kept `PARTIAL` instead of
failing; a reassessed `COMPLETE` with a material gap is still demoted.
Regression: 1,827 tests and 162 subtests passed.

## 6. Acceptance criteria

1. Offline regression green (CI).
2. One LIVE replenishment with all inspection checks PASS, no `COMPLETE`
   research with a material gap, zero side effects and no
   `PROCESSING_FAILED` caused by `PARTIAL_WITHOUT_MORE_RESEARCH`.
3. Reported, not required: how many research received the reassessment and
   what the model decided.
