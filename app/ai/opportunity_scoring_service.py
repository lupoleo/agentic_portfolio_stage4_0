from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any

from pydantic import Field

from app.ai.canonical_technical import CanonicalTechnicalInput

from app.ai.models import (
    AIRequest,
    AITask,
    DataSensitivity,
    ReasoningMode,
    ResponseFormat,
)
from app.ai.opportunity_score_calculator import (
    OpportunityScoreCalculation,
    OpportunityScoreCalculator,
)
from app.ai.opportunity_score_models import OpportunityScoringProfile
from app.ai.opportunity_scoring_ai_models import OpportunityComponentScoringOutput
from app.ai.models import AIModel


class _ComponentAssessmentTransport(AIModel):
    """Permissive LLM transport shape; canonical invariants are applied later."""

    score: float | None = Field(default=None, ge=0.0, le=100.0)
    rationale: str | None = None
    supporting_evidence_ids: list[str] = Field(default_factory=list)


class _OpportunityComponentScoringTransport(AIModel):
    thesis: _ComponentAssessmentTransport
    catalyst: _ComponentAssessmentTransport
    fundamental: _ComponentAssessmentTransport
    technical: _ComponentAssessmentTransport
    expectations: _ComponentAssessmentTransport

    positive_factors: list[str] = Field(default_factory=list)
    negative_factors: list[str] = Field(default_factory=list)
    uncertainty_factors: list[str] = Field(default_factory=list)


class _OpportunityComponentGroundingRepairTransport(AIModel):
    """Input-scoped repair payload for invalid scored components only."""

    thesis: _ComponentAssessmentTransport | None = None
    catalyst: _ComponentAssessmentTransport | None = None
    fundamental: _ComponentAssessmentTransport | None = None
    technical: _ComponentAssessmentTransport | None = None
    expectations: _ComponentAssessmentTransport | None = None


from app.ai.research_models import EvidenceQuality, OpportunityResearch


@dataclass(frozen=True)
class OpportunityScoringServiceResult:
    components: OpportunityComponentScoringOutput
    calculation: OpportunityScoreCalculation
    response: Any
    diagnostics: dict[str, Any]


class OpportunityScoringService:
    """AI semantic component assessment + deterministic score aggregation."""

    def __init__(
        self,
        provider: Any,
        *,
        calculator: OpportunityScoreCalculator | None = None,
        prompt_version: str = "opportunity-scoring-v11-score-rationale-consistency-diagnostics",
        normalize_provider_transport: bool = False,
    ) -> None:
        self.provider = provider
        self.calculator = calculator or OpportunityScoreCalculator()
        self.prompt_version = prompt_version
        self.normalize_provider_transport = normalize_provider_transport

    def score(
        self,
        research: OpportunityResearch,
        *,
        evidence_coverage_score: float,
        scoring_profile: OpportunityScoringProfile = OpportunityScoringProfile.STANDARD,
        sensitivity: DataSensitivity = DataSensitivity.PUBLIC,
        canonical_technical_input: CanonicalTechnicalInput | None = None,
    ) -> OpportunityScoringServiceResult:
        alias_to_canonical, canonical_to_alias = self._build_evidence_alias_maps(
            research.evidence_ids
        )

        scorable_mask = self._build_scorable_mask(research)
        technical_features = self._technical_features_for_scoring(
            research,
            canonical_technical_input,
        )

        request = AIRequest(
            task=AITask.REASONING,
            prompt=self._build_prompt(
                research,
                scoring_profile,
                canonical_to_alias=canonical_to_alias,
                scorable_mask=scorable_mask,
                technical_features=technical_features,
            ),
            sensitivity=sensitivity,
            reasoning_mode=ReasoningMode.REASONING,
            response_format=ResponseFormat.JSON,
            output_schema=_OpportunityComponentScoringTransport,
            metadata={
                "prompt_version": self.prompt_version,
                "ticker": research.ticker,
                "research_id": research.research_id,
                "candidate_id": research.candidate_id,
                "scoring_profile": scoring_profile.value,
            },
        )

        response = self.provider.infer(request)
        if response.structured_output is None:
            raise ValueError("Opportunity scoring requires structured_output")

        initial_model_scores = self._component_score_snapshot(
            response.structured_output
        )

        transport_output = self._apply_bounded_technical_score(
            response.structured_output,
            technical_features,
            scorable_mask.get("technical", False),
        )
        transport_output = self._apply_scorable_mask(
            transport_output,
            scorable_mask,
        )
        missing_scorable = self._find_scorable_components_returned_null(
            transport_output, scorable_mask
        )
        # Explicit transport-normalization mode is a compatibility boundary:
        # it intentionally accepts model NULL explanations and canonicalizes
        # them later. Do not force the v6 scorability repair in that opt-in path.
        scorability_repair_components = set()
        if missing_scorable and not self.normalize_provider_transport:
            scorability_repair_components = set(missing_scorable)
            transport_output = self._repair_missing_scorable_components(
                research=research,
                original_output=transport_output,
                missing_components=missing_scorable,
                canonical_to_alias=canonical_to_alias,
                sensitivity=sensitivity,
            )
        invalid_components = self._find_scored_components_without_evidence(
            transport_output
        )
        grounding_repair_components = set(invalid_components)
        if invalid_components:
            transport_output = self._repair_scored_components_without_evidence(
                research=research,
                original_output=transport_output,
                invalid_components=invalid_components,
                canonical_to_alias=canonical_to_alias,
                sensitivity=sensitivity,
            )

        transport_output = self._null_scores_without_canonical_context(
            research,
            transport_output,
        )

        semantic_calibration_applied = not self.normalize_provider_transport
        if semantic_calibration_applied:
            transport_output = self._apply_nontechnical_semantic_calibration(
                transport_output,
                scorable_mask,
            )

        factor_extraction_applied = not self.normalize_provider_transport
        factor_diagnostics = {
            "applied": False,
            "policy_version": "factor-extraction-v2-grounded-components",
        }

        canonical_input = self._dereference_evidence_aliases(
            transport_output,
            alias_to_canonical,
        )
        if self.normalize_provider_transport:
            canonical_input = self._normalize_component_null_invariants(
                canonical_input
            )
        components = OpportunityComponentScoringOutput.model_validate(
            canonical_input
        )
        self._validate_grounding(research, components)

        if factor_extraction_applied:
            components, factor_diagnostics = (
                self._apply_canonical_factor_extraction_v2(
                    research,
                    components,
                )
            )

        diagnostics = self._build_scorability_provenance(
            research=research,
            scorable_mask=scorable_mask,
            initial_model_scores=initial_model_scores,
            scorability_repair_components=scorability_repair_components,
            grounding_repair_components=grounding_repair_components,
            semantic_calibration_applied=semantic_calibration_applied,
            components=components,
        )
        diagnostics["factor_extraction"] = factor_diagnostics
        diagnostics["score_rationale_consistency"] = (
            self._build_score_rationale_consistency_diagnostics(components)
        )

        calculation = self.calculator.calculate(
            component_scores={
                "thesis_score": components.thesis.score,
                "catalyst_score": components.catalyst.score,
                "fundamental_score": components.fundamental.score,
                "technical_score": components.technical.score,
                "expectations_score": components.expectations.score,
            },
            evidence_quality=research.evidence_quality,
            evidence_coverage_score=evidence_coverage_score,
            research_confidence=research.research_confidence,
            scoring_profile=scoring_profile,
        )

        return OpportunityScoringServiceResult(
            components=components,
            calculation=calculation,
            response=response,
            diagnostics=diagnostics,
        )

    _COMPONENT_NAMES = (
        "thesis",
        "catalyst",
        "fundamental",
        "technical",
        "expectations",
    )

    @classmethod
    def _component_score_snapshot(
        cls,
        structured_output: Any,
    ) -> dict[str, float | None]:
        if hasattr(structured_output, "model_dump"):
            payload = structured_output.model_dump()
        elif isinstance(structured_output, dict):
            payload = dict(structured_output)
        else:
            return {name: None for name in cls._COMPONENT_NAMES}

        snapshot = {}
        for name in cls._COMPONENT_NAMES:
            assessment = payload.get(name)
            if hasattr(assessment, "model_dump"):
                assessment = assessment.model_dump()
            snapshot[name] = (
                assessment.get("score")
                if isinstance(assessment, dict)
                else None
            )
        return snapshot

    @classmethod
    def _component_context_presence(
        cls,
        research: OpportunityResearch,
    ) -> dict[str, bool]:
        thesis_context = any(
            cls._is_substantive_context(value)
            for value in (
                research.market_context,
                research.fundamental_context,
                research.technical_context,
                research.event_context,
                research.catalyst_assessment,
                research.bull_case,
                research.bear_case,
            )
        )
        return {
            "thesis": thesis_context,
            "catalyst": (
                cls._is_substantive_context(research.catalyst_assessment)
                or cls._is_substantive_context(research.event_context)
            ),
            "fundamental": cls._is_substantive_context(
                research.fundamental_context
            ),
            "technical": cls._is_substantive_context(
                research.technical_context
            ),
            "expectations": (
                research.expectations_assessment.value != "UNKNOWN"
            ),
        }

    @classmethod
    def _build_scorability_provenance(
        cls,
        *,
        research: OpportunityResearch,
        scorable_mask: dict[str, bool],
        initial_model_scores: dict[str, float | None],
        scorability_repair_components: set[str],
        grounding_repair_components: set[str],
        semantic_calibration_applied: bool,
        components: OpportunityComponentScoringOutput,
    ) -> dict[str, Any]:
        context_presence = cls._component_context_presence(research)
        details = {}
        for name in cls._COMPONENT_NAMES:
            final_assessment = getattr(components, name)
            details[name] = {
                "context_present": context_presence[name],
                "evidence_present": bool(research.evidence_ids),
                "deterministic_scorable": bool(
                    scorable_mask.get(name, False)
                ),
                "initial_model_score": initial_model_scores.get(name),
                "initial_model_null": (
                    initial_model_scores.get(name) is None
                ),
                "scorability_repair_attempted": (
                    name in scorability_repair_components
                ),
                "grounding_repair_attempted": (
                    name in grounding_repair_components
                ),
                "final_score": final_assessment.score,
                "final_null": final_assessment.score is None,
                "semantic_calibration": (
                    "CATALYST_SHRINK_0.50"
                    if semantic_calibration_applied
                    and name == "catalyst"
                    and scorable_mask.get("catalyst", False)
                    else (
                        "FUNDAMENTAL_SHRINK_0.65"
                        if semantic_calibration_applied
                        and name == "fundamental"
                        and scorable_mask.get("fundamental", False)
                        else "NONE"
                    )
                ),
            }
        return {
            "scorable_mask": dict(scorable_mask),
            "components": details,
        }

    @classmethod
    def _find_scorable_components_returned_null(
        cls, structured_output: Any, scorable_mask: dict[str, bool]
    ) -> list[str]:
        """Find deterministically SCORABLE dimensions returned as null."""
        if hasattr(structured_output, "model_dump"):
            payload = structured_output.model_dump()
        elif isinstance(structured_output, dict):
            payload = dict(structured_output)
        else:
            return []
        missing = []
        for name in cls._COMPONENT_NAMES:
            if not scorable_mask.get(name, False):
                continue
            assessment = payload.get(name)
            if hasattr(assessment, "model_dump"):
                assessment = assessment.model_dump()
            if not isinstance(assessment, dict) or assessment.get("score") is None:
                missing.append(name)
        return missing

    @classmethod
    def _find_scored_components_without_evidence(
        cls,
        structured_output: Any,
    ) -> list[str]:
        """Find numeric components that violate the evidence requirement."""
        if hasattr(structured_output, "model_dump"):
            payload = structured_output.model_dump()
        elif isinstance(structured_output, dict):
            payload = dict(structured_output)
        else:
            return []

        invalid: list[str] = []
        for name in cls._COMPONENT_NAMES:
            assessment = payload.get(name)
            if hasattr(assessment, "model_dump"):
                assessment = assessment.model_dump()
            if not isinstance(assessment, dict):
                continue

            if (
                assessment.get("score") is not None
                and not assessment.get("supporting_evidence_ids")
            ):
                invalid.append(name)

        return invalid

    def _repair_missing_scorable_components(
        self,
        *,
        research: OpportunityResearch,
        original_output: Any,
        missing_components: list[str],
        canonical_to_alias: dict[str, str],
        sensitivity: DataSensitivity,
    ) -> dict[str, Any]:
        """Targeted one-pass repair for SCORABLE dimensions returned as null."""
        if hasattr(original_output, "model_dump"):
            merged = original_output.model_dump()
        elif isinstance(original_output, dict):
            merged = dict(original_output)
        else:
            raise ValueError("Opportunity scoring structured_output must be a mapping or model")

        allowed_aliases = [
            canonical_to_alias.get(evidence_id, evidence_id)
            for evidence_id in research.evidence_ids
        ]
        request = AIRequest(
            task=AITask.REASONING,
            prompt=self._build_missing_scorable_repair_prompt(
                missing_components, allowed_aliases
            ),
            sensitivity=sensitivity,
            reasoning_mode=ReasoningMode.REASONING,
            response_format=ResponseFormat.JSON,
            output_schema=_OpportunityComponentGroundingRepairTransport,
            metadata={
                "prompt_version": f"{self.prompt_version}-scorability-repair-v1",
                "ticker": research.ticker,
                "research_id": research.research_id,
                "candidate_id": research.candidate_id,
                "repair_reason": "SCORABLE_COMPONENT_RETURNED_NULL",
                "repair_components": list(missing_components),
            },
        )
        response = self.provider.infer(request)
        if response.structured_output is None:
            raise ValueError("Opportunity scoring scorability repair requires structured_output")
        repaired = (
            response.structured_output.model_dump()
            if hasattr(response.structured_output, "model_dump")
            else dict(response.structured_output)
        )
        allowed = set(allowed_aliases)
        for name in missing_components:
            assessment = repaired.get(name)
            if hasattr(assessment, "model_dump"):
                assessment = assessment.model_dump()
            if not isinstance(assessment, dict):
                raise ValueError(f"{name} is SCORABLE but targeted repair returned no assessment")
            score = assessment.get("score")
            rationale = assessment.get("rationale")
            cited = assessment.get("supporting_evidence_ids") or []
            if score is None or not rationale or not cited or any(x not in allowed for x in cited):
                raise ValueError(
                    f"{name} is SCORABLE but targeted repair did not produce a grounded numeric assessment"
                )
            merged[name] = assessment
        return merged

    @staticmethod
    def _build_missing_scorable_repair_prompt(
        missing_components: list[str], allowed_aliases: list[str]
    ) -> str:
        components = ", ".join(missing_components)
        aliases = ", ".join(allowed_aliases) or "NONE"
        return f"""Repair ONLY these opportunity-scoring components: {components}.

The deterministic software gate has already established that every named
component is SCORABLE from the supplied canonical research.
ALLOWED EVIDENCE REFERENCES: {aliases}

Return ONLY the requested typed JSON repair object.
For every named component return a numeric score from 0 to 100, a concise
rationale, and one or more exact allowed evidence aliases.
Do not return score=null. Do not modify components not named above.
Do not invent evidence references.
"""

    def _repair_scored_components_without_evidence(
        self,
        *,
        research: OpportunityResearch,
        original_output: Any,
        invalid_components: list[str],
        canonical_to_alias: dict[str, str],
        sensitivity: DataSensitivity,
    ) -> dict[str, Any]:
        """Repair only scored components that omitted supporting evidence.

        Valid original components and factor lists are preserved verbatim.
        The repair may either attach valid evidence aliases or null the
        unsupported component. It gets exactly one model call.
        """
        if hasattr(original_output, "model_dump"):
            merged = original_output.model_dump()
        elif isinstance(original_output, dict):
            merged = dict(original_output)
        else:
            raise ValueError(
                "Opportunity scoring structured_output must be a mapping or model"
            )

        allowed_aliases = [
            canonical_to_alias.get(evidence_id, evidence_id)
            for evidence_id in research.evidence_ids
        ]
        original_subset = {
            name: merged.get(name)
            for name in invalid_components
        }

        repair_request = AIRequest(
            task=AITask.REASONING,
            prompt=self._build_grounding_repair_prompt(
                invalid_components=invalid_components,
                original_subset=original_subset,
                allowed_aliases=allowed_aliases,
            ),
            sensitivity=sensitivity,
            reasoning_mode=ReasoningMode.REASONING,
            response_format=ResponseFormat.JSON,
            output_schema=_OpportunityComponentGroundingRepairTransport,
            metadata={
                "prompt_version": f"{self.prompt_version}-grounding-repair-v1",
                "ticker": research.ticker,
                "research_id": research.research_id,
                "candidate_id": research.candidate_id,
                "repair_reason": "SCORED_COMPONENT_WITHOUT_EVIDENCE",
                "repair_components": list(invalid_components),
            },
        )

        repair_response = self.provider.infer(repair_request)
        if repair_response.structured_output is None:
            raise ValueError(
                "Opportunity scoring grounding repair requires structured_output"
            )

        if hasattr(repair_response.structured_output, "model_dump"):
            repaired = repair_response.structured_output.model_dump()
        elif isinstance(repair_response.structured_output, dict):
            repaired = dict(repair_response.structured_output)
        else:
            raise ValueError(
                "Opportunity scoring grounding repair output must be a mapping or model"
            )

        allowed_alias_set = set(allowed_aliases)

        for name in invalid_components:
            assessment = repaired.get(name)
            if hasattr(assessment, "model_dump"):
                assessment = assessment.model_dump()

            if not isinstance(assessment, dict):
                assessment = {
                    "score": None,
                    "rationale": None,
                    "supporting_evidence_ids": [],
                }

            score = assessment.get("score")
            cited = assessment.get("supporting_evidence_ids") or []
            cited_are_valid = bool(cited) and all(
                value in allowed_alias_set for value in cited
            )

            if score is None or not cited_are_valid:
                assessment["score"] = None
                assessment["rationale"] = None
                assessment["supporting_evidence_ids"] = []

            merged[name] = assessment

        return merged

    @staticmethod
    def _build_grounding_repair_prompt(
        *,
        invalid_components: list[str],
        original_subset: dict[str, Any],
        allowed_aliases: list[str],
    ) -> str:
        components = ", ".join(invalid_components)
        aliases = ", ".join(allowed_aliases) or "NONE"
        prior = json.dumps(
            original_subset,
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        )
        return f"""Repair ONLY these invalid opportunity-scoring components: {components}.

ALLOWED EVIDENCE REFERENCES: {aliases}

The prior output gave a numeric score without supporting evidence:
{prior}

Return ONLY the requested typed JSON repair object.
For each named component, choose exactly one valid action:
1. Keep a numeric score only if you can support it with one or more exact aliases
   from ALLOWED EVIDENCE REFERENCES and a concise rationale.
2. Otherwise return score=null, rationale=null, supporting_evidence_ids=[].

Do not return or modify any component that was not named above.
Do not invent evidence references. Do not return canonical long evidence IDs.
"""

    @staticmethod
    def _is_substantive_context(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            normalized = value.strip().lower()
            return normalized not in {"", "none", "unknown", "n/a"}
        if isinstance(value, (list, tuple, set, dict)):
            return bool(value)
        return True

    @classmethod
    def _build_scorable_mask(
        cls,
        research: OpportunityResearch,
    ) -> dict[str, bool]:
        """Deterministically decide which semantic dimensions may be scored."""
        has_evidence = bool(research.evidence_ids)

        thesis_context = any(
            cls._is_substantive_context(value)
            for value in (
                research.market_context,
                research.fundamental_context,
                research.technical_context,
                research.event_context,
                research.catalyst_assessment,
                research.bull_case,
                research.bear_case,
            )
        )

        return {
            "thesis": has_evidence and thesis_context,
            "catalyst": has_evidence and (
                cls._is_substantive_context(research.catalyst_assessment)
                or cls._is_substantive_context(research.event_context)
            ),
            "fundamental": has_evidence and cls._is_substantive_context(
                research.fundamental_context
            ),
            "technical": has_evidence and cls._is_substantive_context(
                research.technical_context
            ),
            "expectations": has_evidence and (
                research.expectations_assessment.value != "UNKNOWN"
            ),
        }

    @classmethod
    def _apply_scorable_mask(
        cls,
        structured_output: Any,
        scorable_mask: dict[str, bool],
    ) -> dict[str, Any]:
        """Fail closed at component level for dimensions declared unscorable."""
        if hasattr(structured_output, "model_dump"):
            payload = structured_output.model_dump()
        elif isinstance(structured_output, dict):
            payload = dict(structured_output)
        else:
            raise ValueError(
                "Opportunity scoring structured_output must be a mapping or model"
            )

        for name in cls._COMPONENT_NAMES:
            if scorable_mask.get(name, False):
                continue

            assessment = payload.get(name)
            if hasattr(assessment, "model_dump"):
                assessment = assessment.model_dump()
            elif isinstance(assessment, dict):
                assessment = dict(assessment)
            else:
                assessment = {}

            assessment["score"] = None
            assessment["rationale"] = None
            assessment["supporting_evidence_ids"] = []
            payload[name] = assessment

        return payload

    @classmethod
    def _null_scores_without_canonical_context(
        cls,
        research: OpportunityResearch,
        structured_output: Any,
    ) -> dict[str, Any]:
        """Deterministically null scores whose required research context is absent."""
        if hasattr(structured_output, "model_dump"):
            payload = structured_output.model_dump()
        elif isinstance(structured_output, dict):
            payload = dict(structured_output)
        else:
            raise ValueError(
                "Opportunity scoring structured_output must be a mapping or model"
            )

        forced_null: set[str] = set()
        if research.fundamental_context is None:
            forced_null.add("fundamental")
        if research.technical_context is None:
            forced_null.add("technical")
        if (
            research.catalyst_assessment is None
            and research.event_context is None
        ):
            forced_null.add("catalyst")

        for name in forced_null:
            assessment = payload.get(name)
            if hasattr(assessment, "model_dump"):
                assessment = assessment.model_dump()
            elif isinstance(assessment, dict):
                assessment = dict(assessment)
            else:
                assessment = {}

            assessment["score"] = None
            assessment["rationale"] = None
            assessment["supporting_evidence_ids"] = []
            payload[name] = assessment

        return payload

    _SEMANTIC_NEUTRAL_ANCHOR = 50.0
    _CATALYST_RELIABILITY = 0.50
    _FUNDAMENTAL_RELIABILITY = 0.65
    _SEMANTIC_SCORE_GRID = 2.5

    @classmethod
    def _shrink_semantic_score_toward_neutral(
        cls,
        score: float | None,
        *,
        reliability: float,
    ) -> float | None:
        """Deterministically shrink a stochastic semantic score toward neutral.

        Reliability must stay within [0, 1].  A value of 1 preserves the model
        score; 0 collapses it to the neutral anchor.  The result is rounded to
        the same 2.5-point grid used by deterministic Technical calibration.
        """
        if score is None:
            return None
        if not 0.0 <= reliability <= 1.0:
            raise ValueError("semantic calibration reliability must be in [0, 1]")

        calibrated = (
            cls._SEMANTIC_NEUTRAL_ANCHOR
            + reliability * (float(score) - cls._SEMANTIC_NEUTRAL_ANCHOR)
        )
        calibrated = (
            round(calibrated / cls._SEMANTIC_SCORE_GRID)
            * cls._SEMANTIC_SCORE_GRID
        )
        return max(0.0, min(100.0, calibrated))

    @classmethod
    def _apply_nontechnical_semantic_calibration(
        cls,
        structured_output: Any,
        scorable_mask: dict[str, bool],
    ) -> dict[str, Any]:
        """Variance-aware calibration for empirically unstable semantic scores.

        Frozen policy:
        - CATALYST uses reliability 0.50 (AI-8C.3b.5).
        - FUNDAMENTAL uses reliability 0.65 (AI-8C.3d.2).
        - THESIS remains uncalibrated because 8C.3d.1 measured only modest
          true semantic variance.
        Rationale and evidence are preserved verbatim; only numeric scores are
        shrunk toward the neutral anchor on the common 2.5-point grid.
        """
        if hasattr(structured_output, "model_dump"):
            payload = structured_output.model_dump()
        elif isinstance(structured_output, dict):
            payload = dict(structured_output)
        else:
            raise ValueError(
                "Opportunity scoring structured_output must be a mapping or model"
            )

        policies = {
            "catalyst": cls._CATALYST_RELIABILITY,
            "fundamental": cls._FUNDAMENTAL_RELIABILITY,
        }
        for name, reliability in policies.items():
            if not scorable_mask.get(name, False):
                continue
            assessment = payload.get(name)
            if hasattr(assessment, "model_dump"):
                assessment = assessment.model_dump()
            elif isinstance(assessment, dict):
                assessment = dict(assessment)
            else:
                continue

            assessment["score"] = cls._shrink_semantic_score_toward_neutral(
                assessment.get("score"),
                reliability=reliability,
            )
            payload[name] = assessment

        return payload

        assessment = payload.get("catalyst")
        if hasattr(assessment, "model_dump"):
            assessment = assessment.model_dump()
        elif isinstance(assessment, dict):
            assessment = dict(assessment)
        else:
            return payload

        assessment["score"] = cls._shrink_semantic_score_toward_neutral(
            assessment.get("score"),
            reliability=cls._CATALYST_RELIABILITY,
        )
        payload["catalyst"] = assessment
        return payload

    _RATIONALE_SUPPORTIVE_PATTERNS = (
        r"\bstrong\b",
        r"\bpositive\b",
        r"\bsupport(?:s|ed|ive)?\b",
        r"\bgrowth\b",
        r"\bimprov(?:e|ed|ement|ing)\b",
        r"\bbeat(?:s|ing)?\b",
        r"\bupside\b",
        r"\bfavorable\b",
        r"\bbullish\b",
        r"\baccelerat(?:e|ed|ing|ion)\b",
        r"\bbenefit(?:s|ed|ing)?\b",
        r"\bconstructive\b",
        r"\bcredible catalyst(?:s)?\b",
        r"\bpositive catalyst(?:s)?\b",
        r"\bgrowth catalyst(?:s)?\b",
    )
    _RATIONALE_ADVERSE_PATTERNS = (
        r"\bnegative\b",
        r"\badverse\b",
        r"\bweak(?:ness|er)?\b",
        r"\bdeclin(?:e|ed|ing)\b",
        r"\bmiss(?:ed|es|ing)?\b",
        r"\bdownside\b",
        r"\brisk(?:s|y)?\b",
        r"\bbearish\b",
        r"\bpressure\b",
        r"\buncertain(?:ty)?\b",
        r"\bconcern(?:s|ed)?\b",
        r"\bchalleng(?:e|es|ed|ing)\b",
        r"\bslowdown\b",
    )

    @classmethod
    def _rationale_polarity_signals(
        cls,
        rationale: str | None,
    ) -> dict[str, int]:
        text = (rationale or "").strip()
        if not text:
            return {"supportive": 0, "adverse": 0}
        supportive = sum(
            bool(re.search(pattern, text, flags=re.IGNORECASE))
            for pattern in cls._RATIONALE_SUPPORTIVE_PATTERNS
        )
        adverse = sum(
            bool(re.search(pattern, text, flags=re.IGNORECASE))
            for pattern in cls._RATIONALE_ADVERSE_PATTERNS
        )
        return {"supportive": supportive, "adverse": adverse}

    @classmethod
    def _classify_score_rationale_consistency(
        cls,
        score: float | None,
        rationale: str | None,
    ) -> dict[str, object]:
        signals = cls._rationale_polarity_signals(rationale)
        supportive = signals["supportive"]
        adverse = signals["adverse"]

        if score is None or not (rationale or "").strip():
            status = "UNASSESSABLE"
        elif 45.0 <= float(score) <= 55.0:
            status = "NEUTRAL_BAND"
        elif float(score) >= 60.0:
            if adverse > 0 and supportive == 0:
                status = "POSSIBLE_CONTRADICTION"
            elif supportive > 0 and adverse > 0:
                status = "MIXED"
            elif supportive > 0:
                status = "ALIGNED"
            else:
                status = "UNASSESSABLE"
        elif float(score) <= 40.0:
            if supportive > 0 and adverse == 0:
                status = "POSSIBLE_CONTRADICTION"
            elif supportive > 0 and adverse > 0:
                status = "MIXED"
            elif adverse > 0:
                status = "ALIGNED"
            else:
                status = "UNASSESSABLE"
        else:
            status = "UNASSESSABLE"

        return {
            "status": status,
            "score": score,
            "supportive_signals": supportive,
            "adverse_signals": adverse,
        }

    @classmethod
    def _build_score_rationale_consistency_diagnostics(
        cls,
        components: OpportunityComponentScoringOutput,
    ) -> dict[str, object]:
        component_results = {}
        for name in cls._COMPONENT_NAMES:
            assessment = getattr(components, name)
            component_results[name] = (
                cls._classify_score_rationale_consistency(
                    assessment.score,
                    assessment.rationale,
                )
            )

        contradictions = [
            name
            for name, result in component_results.items()
            if result["status"] == "POSSIBLE_CONTRADICTION"
        ]
        return {
            "policy_version": "score-rationale-consistency-v1-diagnostics",
            "diagnostics_only": True,
            "components": component_results,
            "possible_contradictions": contradictions,
            "possible_contradiction_count": len(contradictions),
        }

    _FACTOR_MAX_ITEMS = 5
    _FACTOR_MAX_CHARS = 240

    @classmethod
    def _canonical_factor_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = " ".join(str(value).split()).strip()
        if not text:
            return None
        if len(text) > cls._FACTOR_MAX_CHARS:
            text = text[: cls._FACTOR_MAX_CHARS - 3].rstrip() + "..."
        return text

    @classmethod
    def _canonical_factor_list(
        cls,
        values: list[str | None],
    ) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for value in values:
            text = cls._canonical_factor_text(value)
            if text is None:
                continue
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(text)
            if len(out) >= cls._FACTOR_MAX_ITEMS:
                break
        return out

    @classmethod
    def _canonical_factors_from_research(
        cls,
        research: OpportunityResearch,
    ) -> dict[str, list[str]]:
        """Extract factors only from canonical, governed research fields.

        No new financial interpretation is introduced here:
        - bull_case is explicitly positive framing;
        - key_risks and bear_case are explicitly adverse framing;
        - unknowns and contradictory_evidence are explicitly uncertainty framing.
        """
        positive = cls._canonical_factor_list([
            research.bull_case,
        ])
        negative = cls._canonical_factor_list([
            *research.key_risks,
            research.bear_case,
        ])
        uncertainty = cls._canonical_factor_list([
            *research.unknowns,
            *research.contradictory_evidence,
        ])
        return {
            "positive_factors": positive,
            "negative_factors": negative,
            "uncertainty_factors": uncertainty,
        }

    @classmethod
    def _apply_canonical_factor_extraction_v2(
        cls,
        research: OpportunityResearch,
        components: OpportunityComponentScoringOutput,
    ) -> tuple[OpportunityComponentScoringOutput, dict[str, Any]]:
        """Complete factor lists from governed research plus grounded components.

        Primary sources remain canonical research fields.  When those fields are
        empty, grounded component rationales provide a deterministic fallback:
        score > 55 -> positive, score < 45 -> negative, otherwise uncertainty.
        This avoids a second LLM call and never uses ungrounded free-form factors.
        """
        research_factors = cls._canonical_factors_from_research(research)

        positive = list(research_factors["positive_factors"])
        negative = list(research_factors["negative_factors"])
        uncertainty = list(research_factors["uncertainty_factors"])

        fallback_sources: list[str] = []

        # Fallback is category-scoped and only activates when the governed
        # research fields did not already produce at least one factor for that
        # category. This preserves canonical research as the primary source.
        need_positive = not positive
        need_negative = not negative
        need_uncertainty = not uncertainty

        for name in cls._COMPONENT_NAMES:
            assessment = getattr(components, name)
            if assessment.score is None or not assessment.rationale:
                continue

            score = float(assessment.score)
            rationale = cls._canonical_factor_text(assessment.rationale)
            if rationale is None:
                continue

            if score > 55.0 and need_positive:
                positive.append(rationale)
                fallback_sources.append(f"{name}:positive")
            elif score < 45.0 and need_negative:
                negative.append(rationale)
                fallback_sources.append(f"{name}:negative")
            elif 45.0 <= score <= 55.0 and need_uncertainty:
                uncertainty.append(rationale)
                fallback_sources.append(f"{name}:uncertainty")

        positive = cls._canonical_factor_list(positive)
        negative = cls._canonical_factor_list(negative)
        uncertainty = cls._canonical_factor_list(uncertainty)

        updated = components.model_copy(
            update={
                "positive_factors": positive,
                "negative_factors": negative,
                "uncertainty_factors": uncertainty,
            }
        )

        diagnostics = {
            "applied": True,
            "policy_version": "factor-extraction-v2-grounded-components",
            "final_factor_counts": {
                "positive_factors": len(positive),
                "negative_factors": len(negative),
                "uncertainty_factors": len(uncertainty),
            },
            "primary_source_fields": {
                "positive_factors": ["bull_case"],
                "negative_factors": ["key_risks", "bear_case"],
                "uncertainty_factors": [
                    "unknowns",
                    "contradictory_evidence",
                ],
            },
            "component_fallback_sources": fallback_sources,
        }
        return updated, diagnostics

    @staticmethod
    def _normalize_component_null_invariants(
        structured_output: Any,
    ) -> dict[str, Any]:
        """Conservatively normalize null-score component transport output.

        A model may explain why a component is unscored even though the
        canonical ComponentAssessment contract forbids rationale/evidence when
        score is null. We discard that non-canonical material; we never invent
        or modify a numeric score.
        """
        if hasattr(structured_output, "model_dump"):
            payload = structured_output.model_dump()
        elif isinstance(structured_output, dict):
            payload = dict(structured_output)
        else:
            raise ValueError(
                "Opportunity scoring structured_output must be a mapping or model"
            )

        for name in (
            "thesis",
            "catalyst",
            "fundamental",
            "technical",
            "expectations",
        ):
            assessment = payload.get(name)
            if hasattr(assessment, "model_dump"):
                assessment = assessment.model_dump()
            elif isinstance(assessment, dict):
                assessment = dict(assessment)
            else:
                continue

            if assessment.get("score") is None:
                assessment["rationale"] = None
                assessment["supporting_evidence_ids"] = []

            payload[name] = assessment

        return payload

    @staticmethod
    def _build_evidence_alias_maps(
        evidence_ids: list[str],
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Map canonical evidence IDs to short, stable scoring transport aliases."""
        alias_to_canonical = {
            f"E{index}": evidence_id
            for index, evidence_id in enumerate(evidence_ids, start=1)
        }
        canonical_to_alias = {
            canonical: alias for alias, canonical in alias_to_canonical.items()
        }
        return alias_to_canonical, canonical_to_alias

    @staticmethod
    def _dereference_evidence_aliases(
        structured_output: Any,
        alias_to_canonical: dict[str, str],
    ) -> dict[str, Any]:
        """Dereference exact aliases only; unknown aliases remain fail-closed."""
        if hasattr(structured_output, "model_dump"):
            payload = structured_output.model_dump()
        elif isinstance(structured_output, dict):
            payload = dict(structured_output)
        else:
            raise ValueError(
                "Opportunity scoring structured_output must be a mapping or model"
            )

        for name in (
            "thesis",
            "catalyst",
            "fundamental",
            "technical",
            "expectations",
        ):
            assessment = payload.get(name)
            if hasattr(assessment, "model_dump"):
                assessment = assessment.model_dump()
            elif isinstance(assessment, dict):
                assessment = dict(assessment)
            else:
                continue

            cited = assessment.get("supporting_evidence_ids", [])
            assessment["supporting_evidence_ids"] = [
                alias_to_canonical.get(value, value) for value in cited
            ]
            payload[name] = assessment

        return payload

    @staticmethod
    def _validate_grounding(
        research: OpportunityResearch,
        output: OpportunityComponentScoringOutput,
    ) -> None:
        allowed_ids = set(research.evidence_ids)

        for name in ("thesis", "catalyst", "fundamental", "technical", "expectations"):
            assessment = getattr(output, name)
            unknown = set(assessment.supporting_evidence_ids).difference(allowed_ids)
            if unknown:
                raise ValueError(
                    f"{name} cites evidence not present in OpportunityResearch: "
                    + ", ".join(sorted(unknown))
                )

        # Fail closed on dimensions whose canonical research context is absent.
        if research.fundamental_context is None and output.fundamental.score is not None:
            raise ValueError(
                "fundamental score cannot be produced without fundamental_context"
            )
        if research.technical_context is None and output.technical.score is not None:
            raise ValueError(
                "technical score cannot be produced without technical_context"
            )
        if (
            research.catalyst_assessment is None
            and research.event_context is None
            and output.catalyst.score is not None
        ):
            raise ValueError(
                "catalyst score cannot be produced without catalyst/event context"
            )

    _TECHNICAL_NUMBER_PATTERNS = {
        "return_1d_pct": r"1-session return:\s*([-+]?\d+(?:\.\d+)?)%",
        "return_5d_pct": r"5-session return:\s*([-+]?\d+(?:\.\d+)?)%",
        "return_20d_pct": r"20-session return:\s*([-+]?\d+(?:\.\d+)?)%",
        "close_vs_sma20_pct": r"Close vs SMA20:\s*([-+]?\d+(?:\.\d+)?)%",
        "close_vs_sma50_pct": r"Close vs SMA50:\s*([-+]?\d+(?:\.\d+)?)%",
        "rsi14": r"RSI14:\s*([-+]?\d+(?:\.\d+)?)",
        "rvol": r"RVOL:\s*([-+]?\d+(?:\.\d+)?)x",
    }

    @classmethod
    def _extract_technical_numbers(
        cls,
        technical_context: Any,
    ) -> dict[str, float | None]:
        text = cls._serialize_context_value(technical_context)
        values: dict[str, float | None] = {}

        for name, pattern in cls._TECHNICAL_NUMBER_PATTERNS.items():
            match = re.search(pattern, text, flags=re.IGNORECASE)
            values[name] = float(match.group(1)) if match else None

        return values

    @staticmethod
    def _signed_regime(
        value: float | None,
        *,
        positive_threshold: float,
        negative_threshold: float,
    ) -> str:
        if value is None:
            return "UNKNOWN"
        if value >= positive_threshold:
            return "POSITIVE"
        if value <= negative_threshold:
            return "NEGATIVE"
        return "NEUTRAL"

    @staticmethod
    def _moving_average_regime(value: float | None) -> str:
        if value is None:
            return "UNKNOWN"
        if value > 1.0:
            return "ABOVE_SUPPORTIVE"
        if value < -1.0:
            return "BELOW_ADVERSE"
        return "NEAR_NEUTRAL"

    @staticmethod
    def _rsi_regime(value: float | None) -> str:
        if value is None:
            return "UNKNOWN"
        if value >= 70.0:
            return "OVERBOUGHT_RISK"
        if value > 60.0:
            return "HIGH_NOT_OVERBOUGHT"
        if value >= 40.0:
            return "NEUTRAL"
        if value > 30.0:
            return "LOW_NOT_OVERSOLD"
        return "OVERSOLD"

    @staticmethod
    def _rvol_regime(value: float | None) -> str:
        if value is None:
            return "UNKNOWN"
        if value >= 1.20:
            return "ELEVATED"
        if value <= 0.80:
            return "DEPRESSED"
        return "NORMAL"

    @classmethod
    def _technical_features_for_scoring(
        cls,
        research: OpportunityResearch,
        canonical_technical_input: CanonicalTechnicalInput | None,
    ) -> dict[str, Any]:
        """Use canonical Stage-2 input when supplied; fallback preserves compatibility."""
        if canonical_technical_input is None:
            features = cls._build_canonical_technical_features(
                research.technical_context
            )
            features["technical_input_source"] = "RESEARCH_TEXT_FALLBACK"
            return features

        if canonical_technical_input.ticker != research.ticker:
            raise ValueError(
                "canonical technical ticker must match OpportunityResearch ticker"
            )

        numbers = {
            "return_1d_pct": canonical_technical_input.return_1d_pct,
            "return_5d_pct": canonical_technical_input.return_5d_pct,
            "return_20d_pct": canonical_technical_input.return_20d_pct,
            "close_vs_sma20_pct": canonical_technical_input.close_vs_sma20_pct,
            "close_vs_sma50_pct": canonical_technical_input.close_vs_sma50_pct,
            "rsi14": canonical_technical_input.rsi14,
            "rvol": canonical_technical_input.rvol,
        }

        features: dict[str, Any] = {
            **numbers,
            "momentum_1d": cls._signed_regime(
                numbers["return_1d_pct"],
                positive_threshold=1.0,
                negative_threshold=-1.0,
            ),
            "momentum_5d": cls._signed_regime(
                numbers["return_5d_pct"],
                positive_threshold=2.0,
                negative_threshold=-2.0,
            ),
            "momentum_20d": cls._signed_regime(
                numbers["return_20d_pct"],
                positive_threshold=3.0,
                negative_threshold=-3.0,
            ),
            "trend_vs_sma20": cls._moving_average_regime(
                numbers["close_vs_sma20_pct"]
            ),
            "trend_vs_sma50": cls._moving_average_regime(
                numbers["close_vs_sma50_pct"]
            ),
            "rsi_regime": cls._rsi_regime(numbers["rsi14"]),
            "volume_regime": cls._rvol_regime(numbers["rvol"]),
            "stage2_trend": canonical_technical_input.trend,
            "technical_input_source": canonical_technical_input.source,
        }

        directional = [
            features["momentum_5d"],
            features["momentum_20d"],
            features["trend_vs_sma20"],
            features["trend_vs_sma50"],
        ]
        supportive = sum(
            value in {"POSITIVE", "ABOVE_SUPPORTIVE"}
            for value in directional
        )
        adverse = sum(
            value in {"NEGATIVE", "BELOW_ADVERSE"}
            for value in directional
        )

        if supportive >= 3 and adverse == 0:
            bias = "POSITIVE"
        elif adverse >= 3 and supportive == 0:
            bias = "NEGATIVE"
        elif supportive > adverse:
            bias = "POSITIVE_MIXED"
        elif adverse > supportive:
            bias = "NEGATIVE_MIXED"
        else:
            bias = "MIXED_NEUTRAL"

        features["supportive_signal_count"] = supportive
        features["adverse_signal_count"] = adverse
        features["technical_bias"] = bias
        return features

    @classmethod
    def _build_canonical_technical_features(
        cls,
        technical_context: Any,
    ) -> dict[str, Any]:
        """Canonicalize elementary technical indicators deterministically.

        These are interpretation features, not a Technical Score.
        """
        numbers = cls._extract_technical_numbers(technical_context)

        features: dict[str, Any] = {
            **numbers,
            "momentum_1d": cls._signed_regime(
                numbers["return_1d_pct"],
                positive_threshold=1.0,
                negative_threshold=-1.0,
            ),
            "momentum_5d": cls._signed_regime(
                numbers["return_5d_pct"],
                positive_threshold=2.0,
                negative_threshold=-2.0,
            ),
            "momentum_20d": cls._signed_regime(
                numbers["return_20d_pct"],
                positive_threshold=3.0,
                negative_threshold=-3.0,
            ),
            "trend_vs_sma20": cls._moving_average_regime(
                numbers["close_vs_sma20_pct"]
            ),
            "trend_vs_sma50": cls._moving_average_regime(
                numbers["close_vs_sma50_pct"]
            ),
            "rsi_regime": cls._rsi_regime(numbers["rsi14"]),
            "volume_regime": cls._rvol_regime(numbers["rvol"]),
        }

        directional = [
            features["momentum_5d"],
            features["momentum_20d"],
            features["trend_vs_sma20"],
            features["trend_vs_sma50"],
        ]

        supportive = sum(
            value in {"POSITIVE", "ABOVE_SUPPORTIVE"}
            for value in directional
        )
        adverse = sum(
            value in {"NEGATIVE", "BELOW_ADVERSE"}
            for value in directional
        )

        if supportive >= 3 and adverse == 0:
            bias = "POSITIVE"
        elif adverse >= 3 and supportive == 0:
            bias = "NEGATIVE"
        elif supportive > adverse:
            bias = "POSITIVE_MIXED"
        elif adverse > supportive:
            bias = "NEGATIVE_MIXED"
        else:
            bias = "MIXED_NEUTRAL"

        features["supportive_signal_count"] = supportive
        features["adverse_signal_count"] = adverse
        features["technical_bias"] = bias
        return features

    @classmethod
    def _deterministic_technical_base_score(
        cls,
        features: dict[str, Any],
    ) -> float | None:
        """Map canonical technical regimes to a stable 0-100 base score.

        The base is intentionally simple and auditable. AI may only apply a
        small bounded semantic adjustment after this deterministic mapping.
        """
        directional = (
            features.get("momentum_5d"),
            features.get("momentum_20d"),
            features.get("trend_vs_sma20"),
            features.get("trend_vs_sma50"),
        )
        known = [value for value in directional if value != "UNKNOWN"]
        if not known:
            return None

        score = 50.0
        weights = {
            "POSITIVE": 7.5,
            "NEGATIVE": -7.5,
            "NEUTRAL": 0.0,
            "ABOVE_SUPPORTIVE": 10.0,
            "BELOW_ADVERSE": -10.0,
            "NEAR_NEUTRAL": 0.0,
        }
        for value in known:
            score += weights.get(value, 0.0)

        rsi = features.get("rsi_regime")
        if rsi in {"OVERBOUGHT_RISK", "OVERSOLD"}:
            score -= 2.5

        volume = features.get("volume_regime")
        bias = features.get("technical_bias")
        if volume == "ELEVATED":
            if bias in {"POSITIVE", "POSITIVE_MIXED"}:
                score += 5.0
            elif bias in {"NEGATIVE", "NEGATIVE_MIXED"}:
                score -= 5.0

        return max(0.0, min(100.0, round(score / 2.5) * 2.5))

    @staticmethod
    def _technical_adjustment_from_ai_score(
        ai_score: float | None,
        base_score: float | None,
    ) -> float:
        """Convert model disagreement into one of five bounded adjustments."""
        if ai_score is None or base_score is None:
            return 0.0

        delta = float(ai_score) - float(base_score)
        if delta >= 20.0:
            return 10.0
        if delta >= 7.5:
            return 5.0
        if delta <= -20.0:
            return -10.0
        if delta <= -7.5:
            return -5.0
        return 0.0

    @classmethod
    def _apply_bounded_technical_score(
        cls,
        structured_output: Any,
        technical_features: dict[str, Any],
        technical_is_scorable: bool,
    ) -> dict[str, Any]:
        if hasattr(structured_output, "model_dump"):
            payload = structured_output.model_dump()
        elif isinstance(structured_output, dict):
            payload = dict(structured_output)
        else:
            raise ValueError(
                "Opportunity scoring structured_output must be a mapping or model"
            )

        if not technical_is_scorable:
            return payload

        base = cls._deterministic_technical_base_score(technical_features)
        if base is None:
            return payload

        assessment = payload.get("technical")
        if hasattr(assessment, "model_dump"):
            assessment = assessment.model_dump()
        elif isinstance(assessment, dict):
            assessment = dict(assessment)
        else:
            assessment = {}

        ai_score = assessment.get("score")
        adjustment = cls._technical_adjustment_from_ai_score(ai_score, base)
        final_score = max(0.0, min(100.0, base + adjustment))

        assessment["score"] = final_score
        payload["technical"] = assessment
        return payload

    @staticmethod
    def _format_canonical_technical_features(
        features: dict[str, Any],
    ) -> str:
        ordered = (
            "return_1d_pct",
            "return_5d_pct",
            "return_20d_pct",
            "close_vs_sma20_pct",
            "close_vs_sma50_pct",
            "rsi14",
            "rvol",
            "momentum_1d",
            "momentum_5d",
            "momentum_20d",
            "trend_vs_sma20",
            "trend_vs_sma50",
            "rsi_regime",
            "volume_regime",
            "supportive_signal_count",
            "adverse_signal_count",
            "technical_bias",
            "stage2_trend",
            "technical_input_source",
        )
        return "\n".join(
            f"- {name}: {features.get(name)}"
            for name in ordered
        )

    # Keep the complete scoring prompt below the global 14k character guard
    # after semantic rubrics and canonical technical features are included.
    # Total research-field budget: 5,350 characters.
    _SCORING_CONTEXT_BUDGETS = {
        "market_context": 500,
        "fundamental_context": 800,
        "technical_context": 650,
        "event_context": 650,
        "catalyst_assessment": 650,
        "bull_case": 450,
        "bear_case": 450,
        "key_risks": 400,
        "contradictory_evidence": 400,
        "unknowns": 400,
    }

    @staticmethod
    def _serialize_context_value(value: Any) -> str:
        if value is None:
            return "None"
        if isinstance(value, str):
            return value.strip()
        return json.dumps(value, ensure_ascii=False, default=str)

    @classmethod
    def _bounded_context_value(cls, value: Any, budget: int) -> str:
        """Deterministically compact one research field to a hard char budget.

        Prefer complete sentence/list-like units. If no useful unitization is
        possible, preserve both the beginning and end rather than blindly
        retaining only a prefix.
        """
        text = cls._serialize_context_value(value)
        if len(text) <= budget:
            return text

        # Split on sentence boundaries and common list/newline separators while
        # retaining semantic units. This is deterministic and invokes no model.
        units = [
            unit.strip()
            for unit in re.split(r"(?<=[.!?])\s+|\n+|\s*[;•]\s*", text)
            if unit.strip()
        ]

        if len(units) > 1:
            selected: list[str] = []
            used = 0
            left, right = 0, len(units) - 1
            take_left = True

            # Alternate beginning/end units to preserve thesis plus later
            # caveats/risks instead of prefix-only truncation.
            while left <= right:
                idx = left if take_left else right
                unit = units[idx]
                extra = len(unit) + (1 if selected else 0)
                if used + extra <= budget:
                    if take_left:
                        selected.append(unit)
                        left += 1
                    else:
                        selected.append(unit)
                        right -= 1
                    used += extra
                else:
                    # This unit cannot fit; move past it and keep searching for
                    # smaller complete units that may still fit.
                    if take_left:
                        left += 1
                    else:
                        right -= 1
                take_left = not take_left

            if selected:
                compact = " ".join(selected)
                return compact[:budget]

        # Fallback for one giant unit: preserve both head and tail with an
        # explicit omission marker.
        marker = " ...[bounded]... "
        available = budget - len(marker)
        if available <= 1:
            return text[:budget]
        head = (available + 1) // 2
        tail = available // 2
        return text[:head] + marker + text[-tail:]

    @classmethod
    def _build_bounded_scoring_context(
        cls,
        research: OpportunityResearch,
    ) -> dict[str, str]:
        return {
            field: cls._bounded_context_value(
                getattr(research, field),
                budget,
            )
            for field, budget in cls._SCORING_CONTEXT_BUDGETS.items()
        }

    def _build_prompt(
        self,
        research: OpportunityResearch,
        scoring_profile: OpportunityScoringProfile,
        *,
        canonical_to_alias: dict[str, str] | None = None,
        scorable_mask: dict[str, bool] | None = None,
        technical_features: dict[str, Any] | None = None,
    ) -> str:
        use_transport_aliases = canonical_to_alias is not None
        if use_transport_aliases:
            evidence_aliases = [
                canonical_to_alias.get(evidence_id, evidence_id)
                for evidence_id in research.evidence_ids
            ]
            allowed_evidence_line = (
                "ALLOWED EVIDENCE REFERENCES: "
                + (", ".join(evidence_aliases) or "NONE")
            )
            evidence_reference_rule = (
                "Every numeric component requires a concise rationale and one or "
                "more exact aliases from ALLOWED EVIDENCE REFERENCES."
            )
            evidence_integrity_rule = (
                "Never invent missing fundamentals, technicals, consensus, "
                "catalysts or evidence references. Copy aliases exactly "
                "(for example E1, E2); do not return long canonical evidence IDs."
            )
        else:
            allowed_evidence_line = (
                "ALLOWED EVIDENCE IDS: "
                + (", ".join(research.evidence_ids) or "NONE")
            )
            evidence_reference_rule = (
                "Every numeric component requires a concise rationale and one or "
                "more IDs from ALLOWED EVIDENCE IDS."
            )
            evidence_integrity_rule = (
                "Never invent missing fundamentals, technicals, consensus, "
                "catalysts or evidence IDs."
            )
        if scorable_mask is None:
            scorable_mask = self._build_scorable_mask(research)

        scorable_lines = "\n".join(
            f"- {name}: {'SCORABLE' if scorable_mask[name] else 'MUST_BE_NULL'}"
            for name in self._COMPONENT_NAMES
        )

        context = self._build_bounded_scoring_context(research)
        if technical_features is None:
            technical_features = self._build_canonical_technical_features(
                research.technical_context
            )
        technical_base_score = self._deterministic_technical_base_score(
            technical_features
        )
        technical_feature_lines = self._format_canonical_technical_features(
            technical_features
        )
        return f"""You are the semantic component scorer in a governed investment research pipeline.

SCORING PROFILE: {scoring_profile.value}
TICKER: {research.ticker}
RESEARCH ID: {research.research_id}
RESEARCH STATUS: {research.research_status.value}
EVIDENCE QUALITY: {research.evidence_quality.value}
RESEARCH CONFIDENCE: {research.research_confidence:.4f}
{allowed_evidence_line}

DETERMINISTIC SCORABLE MASK
{scorable_lines}

RESEARCH
Market context: {context["market_context"]}
Fundamental context: {context["fundamental_context"]}
Technical context: {context["technical_context"]}

CANONICAL TECHNICAL FEATURES
{technical_feature_lines}
These canonical feature interpretations are deterministic and authoritative.
When technical_input_source=STAGE2_YAHOO_TECHNICAL, they are the numerical source of truth
and the Research technical narrative must not override them.
Use the raw values for nuance, but do not reinterpret their elementary direction
or regime differently from the canonical labels above.
Deterministic technical base score: {technical_base_score}
For TECHNICAL, your numeric score is treated only as a semantic opinion signal.
Software will convert disagreement versus this base into a bounded adjustment
of -10, -5, 0, +5 or +10 and will compute the final Technical Score.

Event context: {context["event_context"]}
Catalyst assessment: {context["catalyst_assessment"]}
Expectations assessment: {research.expectations_assessment.value}
Bull case: {context["bull_case"]}
Bear case: {context["bear_case"]}
Key risks: {context["key_risks"]}
Contradictory evidence: {context["contradictory_evidence"]}
Unknowns: {context["unknowns"]}

Return ONLY the typed JSON schema requested.

Score each supported dimension from 0 to 100 using ONLY the evidence type
appropriate to that dimension.

SEMANTIC COMPONENT BOUNDARIES
- thesis: overall coherence of the standalone opportunity. It may synthesize
  other supported dimensions, but it must not duplicate a single component.
- catalyst: identifiable events or developments that can plausibly change the
  market's view within the opportunity horizon. General quality, historical
  performance, or vague sector narratives are not catalysts by themselves.
- fundamental: revenue, earnings, margins, cash flow, balance sheet, valuation,
  guidance and other business/economic evidence. Price returns, SMA, RSI, RVOL,
  chart momentum and shareholder-return performance are NOT fundamental evidence.
- technical: price trend, returns, moving averages, momentum, RSI, relative
  volume and other market-price/volume evidence. Fundamentals, guidance and
  business quality are NOT technical evidence.
- expectations: evidence about consensus, positioning, what is priced in,
  surprise versus expectations, implied move, analyst targets/estimates or
  comparable expectation-setting evidence. If expectations research is UNKNOWN,
  this component MUST remain null.

COMMON SCORE ANCHORS
Use scores in increments of 5 whenever possible.
- 0-20: strongly adverse / thesis-disconfirming evidence.
- 25-40: materially weak or negative evidence.
- 45-55: mixed, balanced or only marginally supportive evidence.
- 60-75: clearly supportive evidence with meaningful limitations.
- 80-90: strong, broad and internally consistent support.
- 95-100: exceptional support; reserve for unusually comprehensive evidence.

COMPONENT-SPECIFIC CALIBRATION
- thesis:
  * 45-55 when the case is mixed or depends on one narrow dimension.
  * 60-75 when multiple supported dimensions align but material uncertainty remains.
  * 80+ only when the opportunity thesis is broad, coherent and strongly evidenced.
- catalyst:
  * null when there is no identifiable catalyst/event context.
  * 25-40 for a weak, distant, ambiguous or predominantly adverse catalyst.
  * 45-55 for uncertain/mixed catalyst impact.
  * 60-75 for credible and relevant catalysts with plausible near-term impact.
  * 80+ only for unusually clear, high-impact and well-evidenced catalysts.
- fundamental:
  * 45-55 for mixed or incomplete business evidence.
  * 60-75 for clearly supportive revenue/earnings/margin/cash-flow/guidance evidence.
  * 80+ requires multiple strong fundamental signals, not price performance.
- technical:
  * CANONICAL TECHNICAL FEATURES are authoritative for elementary interpretation.
  * Do not reinterpret ABOVE_SUPPORTIVE as adverse, BELOW_ADVERSE as supportive,
    NEUTRAL RSI as overbought/oversold, or NORMAL volume as elevated/depressed.
  * technical_bias summarizes directional alignment but is NOT itself a score.
  * The deterministic technical base score is the numerical anchor. Do not
    intentionally exaggerate your score to overcome the bounded adjustment.
  * MIXED_NEUTRAL / POSITIVE_MIXED / NEGATIVE_MIXED usually belongs in 45-65,
    depending on strength and conflict.
  * POSITIVE with broad trend/momentum alignment usually belongs in 65-80.
  * NEGATIVE with broad adverse alignment usually belongs in 20-40.
  * 80+ requires broad positive alignment plus meaningful confirming volume/momentum.
  * overbought/oversold regimes are risk/context modifiers, not automatic direction flips.
- expectations:
  * null when expectations are UNKNOWN or unsupported.
  * 45-55 when evidence suggests outcomes are broadly priced in.
  * 60-75 when evidence indicates favorable asymmetry versus expectations.
  * 25-40 when expectations appear demanding or adverse.

Rules:
1. Do NOT calculate raw_score, score_confidence, adjusted score, portfolio fit, position size or CIO decision.
2. {evidence_reference_rule}
3. Obey the DETERMINISTIC SCORABLE MASK exactly. Any MUST_BE_NULL component
   must return score=null, rationale=null, supporting_evidence_ids=[].
4. SCORABLE is a deterministic software decision, not an invitation to decide
   scorability again. Every SCORABLE component MUST return a numeric score,
   concise rationale and supporting evidence aliases. Do not return null for it.
5. {evidence_integrity_rule}
6. Treat UNKNOWN expectations as uncertainty; do not fabricate a neutral score merely to fill the field.
7. Positive, negative and uncertainty factors may be proposed concisely, but
   software owns their final canonical extraction from governed research fields.
"""
