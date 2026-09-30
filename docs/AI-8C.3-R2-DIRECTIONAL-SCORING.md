# AI-8C.3-R2 — Directional Opportunity Scoring

| Field | Value |
| --- | --- |
| Type | Reopening of a frozen contract |
| Reopened contract | AI-8C.3 — Opportunity Scoring |
| Also affected | S2.F scanner research integration (materialization gate, outcome reason) |
| Branch | `e2e-s4.0a-validation-harness` (separate commit) |
| Status | IMPLEMENTED — LIVE ACCEPTANCE PENDING |
| Approved by | Operator, 2026-09-30 |

## 1. Reason

Opportunity Scoring did not receive the hypothesis direction. Its prompt, its
deterministic technical base and its factor extraction all measured support
for a LONG position. Direction was applied only after the eligibility gate,
when a `NEW_SHORT` opportunity took `bear_case` as its thesis.

Evidence from the operator state database (74 persisted `NEW_LONG`/`NEW_SHORT`
pairs on the same listing and integration run):

| Measure | Value |
| --- | ---: |
| Pairs with identical technical score | 63 of 74 |
| Median absolute raw-score difference LONG vs SHORT | 2.86 |
| `NEW_SHORT` with raw score ≥ 60 | 18 |
| Pairs where both LONG and SHORT score raw ≥ 60 | 14 |

A `NEW_SHORT` on a stock the system rated bullish could therefore pass the
score gate. The defect was masked only because research almost never reached
`COMPLETE`, so it had to be fixed before any change that unblocks research.

## 2. Change

1. `OpportunityScoringService.score()` accepts `direction`. The scanner
   integration passes `LONG` for `NEW_LONG` and `SHORT` for `NEW_SHORT`.
   Callers that pass nothing keep the historical LONG semantics, recorded as
   `direction_source = DEFAULT_LONG`.
2. Deterministic technical base: the directional contribution (momentum,
   price versus averages, elevated volume with bias) is mirrored for SHORT; the
   RSI-extreme penalty stays a risk in both directions. LONG results are
   identical to the previous mapping for every feature combination (tested
   exhaustively).
3. The scoring prompt states `HYPOTHESIS DIRECTION` and, for SHORT, the
   directional anchors for thesis, catalyst, fundamental, technical and
   expectations. Both repair prompts carry the direction line.
4. Factor extraction mirrors for SHORT (supporting: `bear_case`, `key_risks`;
   adverse: `bull_case`). Score/rationale consistency diagnostics invert
   polarity for SHORT.
5. Diagnostics persisted in `OpportunityScore.metadata.scoring_diagnostics`
   gain a `direction` block: policy `ai-8c3-directional-scoring-v1`, direction,
   source, LONG-frame and directional technical base, the model's technical
   opinion, and `possible_direction_ignored` when the opinion disagrees with
   the directional base by 20 points or more.
6. Fail-closed materialization gate: a directional hypothesis materializes only
   if its score records `direction_source = HYPOTHESIS` with the same direction.
   Otherwise the outcome is the new reason `SCORE_DIRECTION_MISMATCH`
   (replenishable). This rejects legacy direction-blind scores on resume.
7. Default `prompt_version`: `opportunity-scoring-v13-directional`.

Unchanged: score weights, calculator, confidence formula, the 60 / 0.40
materialization thresholds, research contracts, and all portfolio, risk and
execution contracts.

## 3. Impact analysis

- LONG scoring output changes only through the prompt text (direction line and
  rule); the deterministic LONG technical base is bit-identical.
- Existing `OpportunityScore` records are not rewritten. Their lack of a
  directional block makes them non-materializable under the new gate, which is
  the intended fail-closed behaviour.
- Run and session identities are unaffected: fingerprints do not include
  prompt versions.

Offline verification on the 74 historical pairs, applying only the
deterministic part (technical component mirrored, all other components as
persisted, calculator recomputed and first checked to reproduce every
persisted raw score exactly):

| Measure | Before | After (technical only) |
| --- | ---: | ---: |
| Pairs where both sides score raw ≥ 60 | 14 | 0 |
| `NEW_SHORT` with raw score ≥ 60 | 18 | 6 |
| Median absolute raw-score difference | 2.86 | 8.41 |

The non-technical components (thesis, catalyst, fundamental, expectations)
depend on the model honouring the direction; they can only be verified live.

## 4. Residual risk

A small local model may ignore the direction for the semantic components. The
technical component is protected by construction (deterministic base, bounded
±10 adjustment, `possible_direction_ignored` flag). The semantic components
are not. The acceptance run measures this directly on LONG/SHORT pairs.

Separate finding, not addressed here: both repair prompts are context-free
(they list evidence aliases but not the research content), so a repaired
component is scored with little information.

## 5. Tests

`tests/test_ai_directional_scoring.py`: exhaustive LONG parity with the
pre-R2 mapping, SHORT mirror with RSI risk preserved, prompt/metadata/diagnostics,
default-LONG provenance, bounded and flagged direction disagreement, factor
mirror, polarity inversion, repair prompts, SHORT prompt size guard, the
hypothesis-direction mapping and the materialization gate (missing, opposite,
default and matching direction). Harness inspection gains R2 checks.
Existing fakes and fixtures now declare direction as the real service does.
Regression: 1,653 tests and 162 subtests passed.

## 6. Acceptance criteria

1. Offline regression green on Windows and Linux (CI).
2. Validation harness `Replay` green.
3. One fresh LIVE replenishment session in which:
   - every new directional score records its hypothesis direction with
     source `HYPOTHESIS`;
   - no LONG/SHORT pair on the same listing has both raw scores ≥ 60;
   - zero broker orders, portfolio mutations and automatic executions.
4. Operator review of the per-component pair table in `summary.md`, to judge
   whether the model honours direction for the semantic components.
