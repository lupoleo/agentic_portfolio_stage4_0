from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from app.analysis.technical import analyze_technical
from app.market_data.yahoo_provider import get_price_history


@dataclass(frozen=True)
class CanonicalTechnicalInput:
    """Deterministic technical source-of-truth for opportunity scoring."""

    ticker: str

    current_price: float

    return_1d_pct: float | None
    return_5d_pct: float | None
    return_20d_pct: float | None

    sma20: float
    sma50: float
    close_vs_sma20_pct: float
    close_vs_sma50_pct: float

    rsi14: float
    rvol: float
    trend: str

    source: str = "STAGE2_YAHOO_TECHNICAL"


def _period_return_pct(
    close: pd.Series,
    sessions: int,
) -> float | None:
    """Return current close versus the close N sessions earlier."""
    clean = close.dropna()
    if len(clean) <= sessions:
        return None

    previous = float(clean.iloc[-(sessions + 1)])
    current = float(clean.iloc[-1])

    if previous == 0:
        return None

    return ((current / previous) - 1.0) * 100.0


def canonical_technical_from_history(
    ticker: str,
    history: pd.DataFrame,
) -> CanonicalTechnicalInput:
    """Build canonical technical input from one immutable history frame."""
    technical = analyze_technical(ticker, history)
    close = history["Close"].dropna()

    return CanonicalTechnicalInput(
        ticker=ticker.strip().upper(),
        current_price=float(technical.current_price),
        return_1d_pct=_period_return_pct(close, 1),
        return_5d_pct=_period_return_pct(close, 5),
        return_20d_pct=_period_return_pct(close, 20),
        sma20=float(technical.sma20),
        sma50=float(technical.sma50),
        close_vs_sma20_pct=float(technical.distance_from_sma20_pct),
        close_vs_sma50_pct=float(technical.distance_from_sma50_pct),
        rsi14=float(technical.rsi14),
        rvol=float(technical.relative_volume),
        trend=str(technical.trend),
    )


def build_canonical_technical_input(
    ticker: str,
    *,
    period: str = "1y",
) -> CanonicalTechnicalInput:
    """Fetch Yahoo history once and build Stage-2 canonical technical input."""
    normalized = ticker.strip().upper()
    if not normalized:
        raise ValueError("ticker must not be blank")

    history = get_price_history(
        normalized,
        period=period,
    )

    return canonical_technical_from_history(
        normalized,
        history,
    )
