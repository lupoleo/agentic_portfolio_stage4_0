# AI-8C.2-R5 — Forward-Framed Unknowns and Status Reassessment

| Field | Value |
| --- | --- |
| Type | Revision of a reopened contract (AI-8C.2 research validator and repair) |
| Branch | `ai-8c2-r5-forward-framing` |
| Status | IMPLEMENTED — LIVE ACCEPTANCE PENDING |
| Approved by | Operator, 2026-10-05 (relaxation: future outcomes filed as unknowns no longer block `COMPLETE`) |

## 1. Reason

With news restored (AI-8C.2-R4), the four research of the 2026-10-05 live run
(ENEL.MI, LDO.MI) were all `PARTIAL`. The remaining blocking items were mostly
future outcomes the model filed as unknowns ("Impact of M&A activity on
long-term margins", "Exact future earnings trajectory") or forward items the
guard reclassified as gaps ("Market acceptance of new valuation metrics",
"Volatility from macroeconomic shifts"), plus one sector comparison phrased
without a recognised marker ("Sector-specific pricing multiples not
disclosed").

`PARTIAL` is chosen by the model itself and software never promotes it.
Making these items non-blocking alone would change nothing; the model must
also be asked to reassess.

## 2. Change

1. **Forward-framed unknowns.** An unknown with explicit forward framing
   ("impact of", "future", "sustainability", "trajectory", "outlook",
   "market reaction/acceptance", "potential", "will/could/may", …) and neither a
   missing-data marker ("not provided", "not disclosed", "missing", …) nor
   as-of wording ("latest", "recent", "current", "reported", "historical",
   "consensus", "guidance") is treated as a forward uncertainty: recorded in
   `metadata.forward_framed_unknowns`, not a material gap.
2. Forward markers gain "acceptance", "macroeconomic", "shifts", "pending", so
   such items in `forward_uncertainties` are no longer reclassified as gaps.
3. "Sector-specific" joins the comparison markers of the AI-8C.2-R2
   context-gap rule (a refinement of the already approved principle).
4. **Reassessment.** A `PARTIAL` research with MEDIUM or HIGH evidence quality,
   no material gap after all rules and at least one future outcome (in either
   list) raises the warning `FORWARD_ITEMS_IN_UNKNOWNS`. It triggers one
   field-scoped repair of `unknowns`, `forward_uncertainties`,
   `research_status` and `requires_additional_research`, asking the model to
   move future outcomes to `forward_uncertainties` and to reassess the status
   under rules U4, 16 and 17. The model decides; if it keeps `PARTIAL`, the
   research is kept as is.
5. Metadata records `initial_research_status`,
   `forward_reclassification_requested` and `forward_framed_unknowns`.
   Contract `ai-8c2-research-v4-forward-framing`; the research prompt is
   unchanged.

Unchanged: material terms, missing-data markers, status and quality rules,
scoring, thresholds (60 / 0.40). Software never promotes research to
`COMPLETE`.

## 3. Offline estimate

On the four research of the 2026-10-05 run (ENEL.MI and LDO.MI, LONG and
SHORT), three would have no blocking gap and receive the reassessment; ENEL.MI
`NEW_LONG` keeps one genuine gap ("Full details on balance sheet leverage and
debt structure").

## 4. Residual risk

The framing and as-of lists are heuristics; an as-of gap phrased as a future
impact could pass as forward. The reassessment adds one LLM call to eligible
research. More `COMPLETE` research will reach the unchanged score gate; the
shadow ledger measures whether that matters.

## 5. Tests

`tests/test_ai_research_forward_framing.py` (18 tests): live examples
non-blocking, as-of and missing-data items still blocking, forward items no
longer reclassified, sector-specific multiples as context gap, warning only
without remaining gaps and with MEDIUM/HIGH quality, reassessment when forward
items sit only in their own list, field-scoped repair leading to `COMPLETE`,
and no promotion when the model keeps `PARTIAL`. Regression: 1,816 tests and
162 subtests passed.

## 6. Acceptance criteria

1. Offline regression green (CI).
2. One LIVE replenishment in which eligible research receive the reassessment
   (`forward_reclassification_requested`), no `COMPLETE` research contains a
   material gap, all checks pass and side effects are zero.
3. Reported, not required: the number of `COMPLETE` research and any
   opportunity.
