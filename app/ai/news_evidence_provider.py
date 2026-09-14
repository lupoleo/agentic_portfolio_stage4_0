from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Callable

from app.ai.evidence_provider import (
    EvidenceFetchResult,
    EvidenceFetchStatus,
    EvidenceItem,
    EvidenceKind,
    EvidenceProvider,
    EvidenceProviderResponseError,
    EvidenceRequest,
    EvidenceSource,
)
from app.ai.research_service import ResearchEvidence


NewsLoader = Callable[[str], Any]

NEWS_SELECTION_POLICY_VERSION = "yahoo-news-selection-v2-canonical"


class YahooNewsEvidenceProvider(EvidenceProvider):
    """News/event evidence using Yahoo Finance's public ticker news feed.

    This provider only retrieves and normalizes evidence. It performs no
    LLM inference and does not decide whether an item is bullish or bearish.
    """

    def __init__(
        self,
        news_loader: NewsLoader | None = None,
        *,
        default_lookback_days: int = 30,
    ) -> None:
        self._news_loader = news_loader or self._default_news_loader
        self.default_lookback_days = default_lookback_days

    @property
    def provider_name(self) -> str:
        return "YAHOO_NEWS"

    def fetch(self, request: EvidenceRequest) -> EvidenceFetchResult:
        fetched_at = datetime.now(timezone.utc)

        try:
            raw_items = self._news_loader(request.ticker)
        except Exception as exc:
            raise EvidenceProviderResponseError(
                f"Yahoo news retrieval failed for {request.ticker}: {exc}"
            ) from exc

        normalized = self._normalize_collection(raw_items)
        if not normalized:
            return EvidenceFetchResult(
                provider=self.provider_name,
                ticker=request.ticker,
                status=EvidenceFetchStatus.NO_DATA,
                items=[],
                warnings=["No usable news/event evidence returned"],
                fetched_at=fetched_at,
            )

        cutoff = None
        if request.as_of is not None:
            as_of = self._ensure_utc(request.as_of)
            from datetime import timedelta
            cutoff = as_of - timedelta(days=self.default_lookback_days)

        # AI-8C.3b.8: Yahoo feed order is not a stable selection contract.
        # Normalize the complete candidate set, apply the as-of window, then
        # canonicalize order before deduplication and max_items truncation.
        candidates: list[dict[str, Any]] = []
        for raw in normalized:
            parsed = self._parse_item(raw, request.ticker, fetched_at)
            if parsed is None:
                continue

            published_at = parsed["published_at"]
            if request.as_of is not None:
                as_of = self._ensure_utc(request.as_of)
                if published_at is not None and published_at > as_of:
                    continue
                if cutoff is not None and published_at is not None and published_at < cutoff:
                    continue
            candidates.append(parsed)

        candidates.sort(key=self._canonical_sort_key)

        output: list[EvidenceItem] = []
        seen: set[str] = set()
        for parsed in candidates:
            dedup_key = self._dedup_key(parsed)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            output.append(
                self._build_item(
                    ticker=request.ticker,
                    parsed=parsed,
                    fetched_at=fetched_at,
                )
            )
            if len(output) >= request.max_items:
                break

        if not output:
            return EvidenceFetchResult(
                provider=self.provider_name,
                ticker=request.ticker,
                status=EvidenceFetchStatus.NO_DATA,
                items=[],
                warnings=["News was returned but no usable items passed normalization/filtering"],
                fetched_at=fetched_at,
            )

        return EvidenceFetchResult(
            provider=self.provider_name,
            ticker=request.ticker,
            status=EvidenceFetchStatus.SUCCESS,
            items=output,
            fetched_at=fetched_at,
            metadata={
                "lookback_days": self.default_lookback_days,
                "selection_policy_version": NEWS_SELECTION_POLICY_VERSION,
                "normalized_candidate_count": len(normalized),
                "eligible_candidate_count": len(candidates),
                "selected_evidence_ids": [
                    item.evidence.evidence_id for item in output
                ],
            },
        )

    @staticmethod
    def _default_news_loader(ticker: str) -> Any:
        try:
            import yfinance as yf
        except ImportError as exc:
            raise EvidenceProviderResponseError(
                "yfinance is required for YahooNewsEvidenceProvider"
            ) from exc
        return yf.Ticker(ticker).news

    @staticmethod
    def _normalize_collection(raw_items: Any) -> list[dict[str, Any]]:
        if raw_items is None:
            return []
        if isinstance(raw_items, list):
            return [x for x in raw_items if isinstance(x, dict)]
        raise EvidenceProviderResponseError(
            "Unsupported Yahoo news result type"
        )

    def _parse_item(
        self,
        raw: dict[str, Any],
        ticker: str,
        fetched_at: datetime,
    ) -> dict[str, Any] | None:
        # yfinance has used both flat and nested ("content") news shapes.
        content = raw.get("content")
        data = content if isinstance(content, dict) else raw

        title = self._first_text(data, "title")
        if not title:
            return None

        summary = self._first_text(
            data, "summary", "description", "snippet"
        )

        publisher = self._publisher(data) or self._publisher(raw) or "Yahoo Finance feed"
        url = self._url(data) or self._url(raw)
        published_at = (
            self._published_at(data)
            or self._published_at(raw)
            or fetched_at
        )

        text = f"Headline: {title}"
        if summary:
            text += f". Summary: {summary}"

        return {
            "title": title,
            "summary": summary,
            "publisher": publisher,
            "url": url,
            "published_at": published_at,
            "text": text,
            "ticker": ticker,
        }

    @staticmethod
    def _first_text(data: dict[str, Any], *keys: str) -> str | None:
        for key in keys:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _publisher(data: dict[str, Any]) -> str | None:
        for key in ("publisher", "provider", "source"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                for nested in ("displayName", "name"):
                    text = value.get(nested)
                    if isinstance(text, str) and text.strip():
                        return text.strip()
        return None

    @staticmethod
    def _url(data: dict[str, Any]) -> str | None:
        for key in ("link", "url"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        canonical = data.get("canonicalUrl")
        if isinstance(canonical, dict):
            value = canonical.get("url")
            if isinstance(value, str) and value.strip():
                return value.strip()

        click = data.get("clickThroughUrl")
        if isinstance(click, dict):
            value = click.get("url")
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _published_at(self, data: dict[str, Any]) -> datetime | None:
        for key in ("providerPublishTime", "pubDate", "published_at", "publishedAt"):
            value = data.get(key)
            parsed = self._parse_datetime(value)
            if parsed is not None:
                return parsed
        return None

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return YahooNewsEvidenceProvider._ensure_utc(value)
        if isinstance(value, (int, float)):
            try:
                return datetime.fromtimestamp(value, tz=timezone.utc)
            except (ValueError, OSError, OverflowError):
                return None
        if isinstance(value, str) and value.strip():
            text = value.strip().replace("Z", "+00:00")
            try:
                return YahooNewsEvidenceProvider._ensure_utc(
                    datetime.fromisoformat(text)
                )
            except ValueError:
                return None
        return None

    @staticmethod
    def _ensure_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _canonical_sort_key(parsed: dict[str, Any]) -> tuple[float, str, str]:
        published_at = parsed["published_at"]
        timestamp = published_at.timestamp() if published_at is not None else 0.0
        return (
            -timestamp,
            parsed["title"].strip().casefold(),
            (parsed["url"] or "").strip().casefold(),
        )

    @staticmethod
    def _dedup_key(parsed: dict[str, Any]) -> str:
        base = "|".join(
            [
                parsed["title"].strip().lower(),
                (parsed["url"] or "").strip().lower(),
            ]
        )
        return sha256(base.encode("utf-8")).hexdigest()

    def _build_item(
        self,
        *,
        ticker: str,
        parsed: dict[str, Any],
        fetched_at: datetime,
    ) -> EvidenceItem:
        published_at = parsed["published_at"]
        digest_input = "|".join(
            [
                ticker,
                published_at.isoformat(),
                parsed["title"],
                parsed["url"] or "",
            ]
        )
        digest = sha256(digest_input.encode("utf-8")).hexdigest()[:12]

        evidence_id = f"EVID-YNEWS-{ticker}-{digest}"
        source_id = f"SRC-YNEWS-{ticker}-{digest}"

        source = EvidenceSource(
            source_id=source_id,
            provider=self.provider_name,
            source_type="NEWS_EVENT",
            source_name=parsed["publisher"],
            source_url=parsed["url"],
            retrieved_at=fetched_at,
            published_at=published_at,
            metadata={"ticker": ticker, "headline": parsed["title"]},
        )

        evidence = ResearchEvidence(
            evidence_id=evidence_id,
            source_type="NEWS_EVENT",
            text=parsed["text"],
            published_at=published_at,
            metadata={
                "provider": self.provider_name,
                "source_id": source_id,
                "publisher": parsed["publisher"],
                "url": parsed["url"],
            },
        )

        return EvidenceItem(
            evidence=evidence,
            source=source,
            kind=EvidenceKind.NEWS,
            ticker=ticker,
            metadata={"headline": parsed["title"]},
        )
