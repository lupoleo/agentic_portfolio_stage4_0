"""AI-8C.2-R6: PARTIAL research must name what is missing (operator decision 2026-10-08)."""
from __future__ import annotations

from app.ai.research_models import EvidenceQuality, ResearchStatus
from app.ai.research_service import RESEARCH_CONTRACT_VERSION, ResearchService
from app.ai.research_validator import ResearchCoverageCode, ResearchCoverageValidator
from tests.test_ai_research_context_gaps import FULL
from tests.test_ai_research_forward_uncertainties import output
from tests.test_ai_research_service_coverage_integration import NOW, SequenceProvider, candidate


def codes(value):
    report = ResearchCoverageValidator().validate(value, list(FULL))
    return {issue.code for issue in report.issues}


def partial(**overrides):
    data = dict(research_status=ResearchStatus.PARTIAL, requires_additional_research=True,
                evidence_quality=EvidenceQuality.HIGH, research_confidence=0.5,
                unknowns=[], forward_uncertainties=[])
    data.update(overrides)
    return output(**data)


def test_contract_version():
    assert RESEARCH_CONTRACT_VERSION == "ai-8c2-research-v5-named-gaps"


def test_partial_naming_nothing_warns():
    # Live 2026-10-08, A2A.MI NEW_SHORT: PARTIAL, HIGH, no unknown at all.
    issues = ResearchCoverageValidator().validate(partial(), list(FULL)).issues
    named = [i for i in issues if i.code is ResearchCoverageCode.PARTIAL_WITHOUT_NAMED_GAP]
    assert len(named) == 1 and "no unknowns are listed" in named[0].message


def test_partial_with_context_gaps_only_warns():
    value = partial(unknowns=["Peer valuation benchmarks"])
    assert ResearchCoverageCode.PARTIAL_WITHOUT_NAMED_GAP in codes(value)


def test_partial_with_a_material_gap_does_not_warn():
    value = partial(unknowns=["Full details on balance sheet leverage and debt structure"])
    assert ResearchCoverageCode.PARTIAL_WITHOUT_NAMED_GAP not in codes(value)


def test_forward_items_keep_the_r5_reassessment_only():
    value = partial(forward_uncertainties=["Sustainability of revenue growth"])
    found = codes(value)
    assert ResearchCoverageCode.FORWARD_ITEMS_IN_UNKNOWNS in found
    assert ResearchCoverageCode.PARTIAL_WITHOUT_NAMED_GAP not in found


def test_low_quality_partial_does_not_warn():
    value = partial(evidence_quality=EvidenceQuality.LOW)
    assert ResearchCoverageCode.PARTIAL_WITHOUT_NAMED_GAP not in codes(value)


def test_complete_does_not_warn():
    value = output(unknowns=[], forward_uncertainties=[])
    assert ResearchCoverageCode.PARTIAL_WITHOUT_NAMED_GAP not in codes(value)


def test_model_may_reassess_to_complete():
    provider = SequenceProvider([partial(), output(unknowns=[], forward_uncertainties=[])])
    result = ResearchService(provider).research(candidate(), list(FULL), now=NOW)
    assert len(provider.requests) == 2
    assert {"unknowns", "research_status", "requires_additional_research"} <= set(
        provider.requests[1].output_schema.model_fields)
    assert "PARTIAL STATUS REASSESSMENT" in provider.requests[1].prompt
    research = result.research
    assert research.research_status is ResearchStatus.COMPLETE
    assert research.metadata["named_gap_reassessment_requested"] is True
    assert research.metadata["initial_research_status"] == "PARTIAL"
    assert research.metadata["research_contract"] == RESEARCH_CONTRACT_VERSION


def test_model_may_name_the_missing_fact_and_keep_partial():
    gap = "Latest free cash flow figures"
    provider = SequenceProvider([partial(), partial(unknowns=[gap])])
    research = ResearchService(provider).research(candidate(), list(FULL), now=NOW).research
    assert research.research_status is ResearchStatus.PARTIAL
    assert research.unknowns == [gap]


def test_software_never_promotes_when_the_model_keeps_partial():
    provider = SequenceProvider([partial(), partial()])
    research = ResearchService(provider).research(candidate(), list(FULL), now=NOW).research
    assert research.research_status is ResearchStatus.PARTIAL
    assert research.requires_additional_research is True


def test_partial_without_more_research_is_kept_partial_instead_of_failing():
    # Live 2026-10-08, A2A.MI NEW_LONG: PARTIAL with requires_additional_research
    # false before and after the repair -> PROCESSING_FAILED before R6.
    incoherent = partial(requires_additional_research=False)
    provider = SequenceProvider([incoherent, incoherent])
    research = ResearchService(provider).research(candidate(), list(FULL), now=NOW).research
    assert research.research_status is ResearchStatus.PARTIAL
    assert research.requires_additional_research is True
    assert research.metadata["deterministic_status_normalized"] is True


def test_complete_with_a_material_gap_after_reassessment_is_still_demoted():
    gap = "Full details on balance sheet leverage and debt structure"
    provider = SequenceProvider([
        partial(),
        output(unknowns=[gap], forward_uncertainties=[]),
    ])
    research = ResearchService(provider).research(candidate(), list(FULL), now=NOW).research
    assert research.research_status is ResearchStatus.PARTIAL
