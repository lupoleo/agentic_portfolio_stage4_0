from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Iterable

from app.ai.research_models import EvidenceQuality, ResearchStatus

if TYPE_CHECKING:
    from app.ai.research_service import ResearchEvidence, ResearchModelOutput


class ResearchCoverageSeverity(str, Enum):
    ERROR = "ERROR"
    WARNING = "WARNING"


class ResearchCoverageCode(str, Enum):
    TECHNICAL_CONTEXT_MISSING = "TECHNICAL_CONTEXT_MISSING"
    EVENT_CONTEXT_MISSING = "EVENT_CONTEXT_MISSING"
    COMPLETE_WITH_LOW_EVIDENCE = "COMPLETE_WITH_LOW_EVIDENCE"
    COMPLETE_WITH_MATERIAL_UNKNOWNS = "COMPLETE_WITH_MATERIAL_UNKNOWNS"
    COMPLETE_REQUIRES_MORE = "COMPLETE_REQUIRES_MORE"
    INSUFFICIENT_WITHOUT_MORE_RESEARCH = "INSUFFICIENT_WITHOUT_MORE_RESEARCH"
    UNKNOWN_CONTRADICTS_SUPPLIED_FACT = "UNKNOWN_CONTRADICTS_SUPPLIED_FACT"
    CONTRADICTION_LOOKS_LIKE_RISK = "CONTRADICTION_LOOKS_LIKE_RISK"


@dataclass(frozen=True)
class ResearchCoverageIssue:
    code: ResearchCoverageCode
    severity: ResearchCoverageSeverity
    message: str


@dataclass(frozen=True)
class ResearchCoverageReport:
    issues: tuple[ResearchCoverageIssue, ...]

    @property
    def errors(self):
        return tuple(i for i in self.issues if i.severity == ResearchCoverageSeverity.ERROR)

    @property
    def warnings(self):
        return tuple(i for i in self.issues if i.severity == ResearchCoverageSeverity.WARNING)

    @property
    def is_valid(self) -> bool:
        return not self.errors

    def repair_instructions(self) -> str:
        if self.is_valid:
            return ""
        lines = [
            "The previous structured research output violates the research coverage contract.",
            "Repair the structured output using ONLY the supplied evidence.",
            "Do not browse, invent, or add facts.",
            "Correct these issues:",
        ]
        for issue in self.errors:
            lines.append(f"- {issue.code.value}: {issue.message}")
        lines += [
            "Return the complete structured object again.",
            "Preserve supported analysis, but move content into the correct structured fields.",
            "Do not make a trade recommendation.",
        ]
        return "\n".join(lines)


class ResearchCoverageValidator:
    _TECHNICAL_TERMS = (
        "rsi", "sma", "moving average", "return", "rvol", "relative volume",
        "volume", "volatility", "close above", "close below", "trend",
    )
    _EVENT_SOURCE_TERMS = (
        "news", "event", "earnings", "guidance", "analyst", "rating",
        "product", "customer", "regulatory", "m&a", "macro",
    )
    _MATERIAL_UNKNOWN_TERMS = (
        "valuation", "revenue", "earnings", "margin", "cash flow",
        "balance sheet", "consensus", "whisper", "expectation",
        "market_context", "market context", "sector", "peer",
        "volatility", "guidance",
    )
    _RISK_LIKE_TERMS = (
        "overbought", "oversold", "pullback", "correction", "risk",
        "valuation", "caution", "volatile", "volatility", "reversal",
    )

    def validate(
        self,
        output: "ResearchModelOutput",
        evidence: Iterable["ResearchEvidence"],
    ) -> ResearchCoverageReport:
        evidence_list = list(evidence)
        evidence_blob = " ".join(f"{x.source_type} {x.text}" for x in evidence_list).lower()
        issues = []

        if self._has_any(evidence_blob, self._TECHNICAL_TERMS) and not self._meaningful(output.technical_context):
            issues.append(ResearchCoverageIssue(
                ResearchCoverageCode.TECHNICAL_CONTEXT_MISSING,
                ResearchCoverageSeverity.ERROR,
                "Material technical evidence is supplied, but technical_context is empty.",
            ))

        if self._has_event_evidence(evidence_list) and not self._meaningful(output.event_context):
            issues.append(ResearchCoverageIssue(
                ResearchCoverageCode.EVENT_CONTEXT_MISSING,
                ResearchCoverageSeverity.ERROR,
                "Material news/event evidence is supplied, but event_context is empty.",
            ))

        if output.research_status == ResearchStatus.COMPLETE and output.evidence_quality == EvidenceQuality.LOW:
            issues.append(ResearchCoverageIssue(
                ResearchCoverageCode.COMPLETE_WITH_LOW_EVIDENCE,
                ResearchCoverageSeverity.ERROR,
                "COMPLETE is incompatible with LOW evidence quality.",
            ))

        if output.research_status == ResearchStatus.COMPLETE and output.requires_additional_research:
            issues.append(ResearchCoverageIssue(
                ResearchCoverageCode.COMPLETE_REQUIRES_MORE,
                ResearchCoverageSeverity.ERROR,
                "COMPLETE cannot require additional research.",
            ))

        if output.research_status == ResearchStatus.INSUFFICIENT_EVIDENCE and not output.requires_additional_research:
            issues.append(ResearchCoverageIssue(
                ResearchCoverageCode.INSUFFICIENT_WITHOUT_MORE_RESEARCH,
                ResearchCoverageSeverity.ERROR,
                "INSUFFICIENT_EVIDENCE must require additional research.",
            ))

        material_unknowns = [u for u in output.unknowns if self._has_any(u.lower(), self._MATERIAL_UNKNOWN_TERMS)]
        if output.research_status == ResearchStatus.COMPLETE and material_unknowns:
            issues.append(ResearchCoverageIssue(
                ResearchCoverageCode.COMPLETE_WITH_MATERIAL_UNKNOWNS,
                ResearchCoverageSeverity.ERROR,
                "COMPLETE contains material unknowns: " + "; ".join(material_unknowns[:5]),
            ))

        for value in output.contradictory_evidence or []:
            low = value.lower()
            if self._has_any(low, self._RISK_LIKE_TERMS) and not self._looks_like_tension(low):
                issues.append(ResearchCoverageIssue(
                    ResearchCoverageCode.CONTRADICTION_LOOKS_LIKE_RISK,
                    ResearchCoverageSeverity.WARNING,
                    f"Possible risk placed in contradictory_evidence: {value}",
                ))

        for unknown in output.unknowns:
            if self._unknown_conflicts_with_evidence(unknown, evidence_blob):
                issues.append(ResearchCoverageIssue(
                    ResearchCoverageCode.UNKNOWN_CONTRADICTS_SUPPLIED_FACT,
                    ResearchCoverageSeverity.ERROR,
                    f"Unknown appears to contradict supplied evidence: {unknown}",
                ))

        return ResearchCoverageReport(tuple(issues))

    @staticmethod
    def _meaningful(value):
        return bool(value and value.strip())

    @staticmethod
    def _has_any(text, terms):
        return any(term in text for term in terms)

    def _has_event_evidence(self, evidence):
        for item in evidence:
            source = item.source_type.lower()
            text = item.text.lower()
            if self._has_any(source, self._EVENT_SOURCE_TERMS):
                return True
            if self._has_any(text, ("earnings", "guidance", "analyst", "deployment", "customer", "launch", "acquisition", "rating")):
                return True
        return False

    @staticmethod
    def _looks_like_tension(text):
        return any(token in text for token in ("despite", "while", "although", "but", "however", "coexist", "versus", "vs.", "contradict"))

    @staticmethod
    def _unknown_conflicts_with_evidence(unknown, evidence_blob):
        low = unknown.lower()

        # An unknown about the consequence, impact, interpretation or future
        # effect of a supplied fact does not contradict the fact itself.
        implication_qualifiers = (
            "impact",
            "effect",
            "implication",
            "consequence",
            "future",
            "outcome",
            "price performance",
            "price reaction",
            "market reaction",
        )
        if any(term in low for term in implication_qualifiers):
            return False

        families = {
            "rsi": ("rsi",),
            "moving average": ("sma", "moving average"),
            "sma": ("sma", "moving average"),
            "rvol": ("rvol", "relative volume"),
            "relative volume": ("rvol", "relative volume"),
            "return": ("return",),
            "price performance": ("return", "price"),
        }
        for token, evidence_tokens in families.items():
            if token in low and any(x in evidence_blob for x in evidence_tokens):
                if any(q in low for q in ("beyond", "longer-term", "other", "additional")):
                    return False
                return True
        return False
