# AI-8C.3-R2.2 — Shared, Direction-Free Company Assessment

| Field | Value |
| --- | --- |
| Type | Revision of a reopened contract (AI-8C.3 Opportunity Scoring) |
| Also affected | Stage 4 runtime wiring; S2.F materialization gate for SHORT |
| Branch | `e2e-s4.0a-validation-harness` (separate commit) |
| Status | IMPLEMENTED — LIVE ACCEPTANCE PENDING; SHORT still suspended |
| Approved by | Operator, 2026-09-30 |

## 1. Reason

After R2.1, FUNDAMENTAL and EXPECTATIONS were meant to be scored in the
company frame and mirrored for SHORT. Three live runs showed the company-frame
values of the same listing still differing between the LONG and SHORT sides by
up to 22.5 points, in both directions (BAMI fundamental 42.5 vs 65 and 50 vs
72.5; HPE 62.5 vs 47.5; NIO expectations 50 vs 65; BMW expectations 50 vs 78).
Two causes: the hypothesis direction in the scoring prompt contaminates the
company judgement, and each hypothesis has its own research text.

## 2. Change

1. New `CompanyAssessmentService` (`app/ai/company_assessment.py`): one
   direction-free LLM call per listing scores FUNDAMENTAL and EXPECTATIONS from
   the FUNDAMENTAL and ANALYST evidence only. The prompt contains no direction.
   Policy `ai-8c3-company-assessment-v1`, prompt
   `company-assessment-v1-direction-free`.
2. The assessment is cached by a fingerprint of the ticker and the
   content-addressed evidence IDs, so the LONG and SHORT hypotheses of a
   listing, which receive identical evidence, share one assessment and one
   call.
3. `OpportunityScoringService` (when constructed with the service, as the
   Stage 4 runtime and the live integration tool now do) replaces the scoring
   model's FUNDAMENTAL and EXPECTATIONS with the shared values, before the
   existing semantic calibration and the SHORT mirror. It does so only for
   SCORABLE components whose shared score cites evidence present in that
   research; otherwise the scoring model's value is kept and the source says
   so (`SCORING_MODEL`, `SCORING_MODEL_SHARED_NOT_GROUNDED`).
4. Fail-closed: no company evidence, a provider failure or an ungrounded shared
   score leave the scoring model's values in place; they never raise.
5. Diagnostics (persisted in the score) record the shared assessment, its
   fingerprint, the per-component source and the scoring model's own values.
   Directional policy becomes `ai-8c3-directional-scoring-v3`; materialization
   accepts only v3 scores.
6. When SHORT materialization is re-enabled, a SHORT score materializes only if
   every scored company-frame component came from the shared assessment
   (otherwise `SCORE_DIRECTION_MISMATCH`). SHORT stays suspended in this
   commit; re-enabling it is a separate operator decision after live
   acceptance.

Unchanged: the scoring prompt, weights, calculator, calibration, thresholds
and the LONG path except for the source of its company-frame values.

Cost: one additional LLM call per listing (not per hypothesis).

## 3. Residual risk

Two direction-free calls on the same evidence are still stochastic, so a
resumed run that loses the in-memory cache may compute a slightly different
assessment for the second hypothesis. The contamination by direction, the
dominant cause, is removed in every case.

## 4. Tests

`tests/test_ai_company_assessment.py` (11 tests): evidence selection and a
direction-free prompt, caching by evidence, no call without company evidence,
provider failure, ungrounded scores, LONG and SHORT sharing one assessment with
identical company frames and mirrored finals despite a self-contradicting
scoring model, not-grounded fallback, unscorable components, disabled service,
the SHORT gate and the runtime wiring. Harness: company-frame identity check
within pairs sharing an assessment. Regression: 1,742 tests and 162 subtests
passed.

## 5. Acceptance criteria

1. Offline regression green (CI) and Replay green.
2. One fresh LIVE session in which every directional score uses
   `ai-8c3-directional-scoring-v3`, company-frame values are identical within
   every LONG/SHORT pair sharing an assessment, and side effects are zero.
3. Operator decision on re-enabling SHORT materialization.
