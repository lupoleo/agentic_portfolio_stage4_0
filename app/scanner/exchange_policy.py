from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ExchangeCoverageScope(str, Enum):
    FULL_EXCHANGE = "FULL_EXCHANGE"
    INDEX_FALLBACK = "INDEX_FALLBACK"


@dataclass(frozen=True)
class EnabledExchange:
    exchange_code: str
    provider_id: str
    coverage_scope: ExchangeCoverageScope
    enabled: bool = True

    def __post_init__(self) -> None:
        exchange_code = self.exchange_code.strip().upper()
        provider_id = self.provider_id.strip()

        if not exchange_code:
            raise ValueError("exchange_code must not be blank")
        if not provider_id:
            raise ValueError("provider_id must not be blank")

        try:
            coverage_scope = ExchangeCoverageScope(
                self.coverage_scope
            )
        except ValueError as exc:
            raise ValueError(
                f"unsupported coverage_scope: "
                f"{self.coverage_scope!r}"
            ) from exc

        object.__setattr__(
            self,
            "exchange_code",
            exchange_code,
        )
        object.__setattr__(
            self,
            "provider_id",
            provider_id,
        )
        object.__setattr__(
            self,
            "coverage_scope",
            coverage_scope,
        )


@dataclass(frozen=True)
class EnabledExchangePolicy:
    entries: tuple[EnabledExchange, ...]
    policy_id: str = "scanner-v1"
    policy_version: str = "1"

    def __post_init__(self) -> None:
        policy_id = self.policy_id.strip()
        policy_version = self.policy_version.strip()

        if not policy_id:
            raise ValueError("policy_id must not be blank")
        if not policy_version:
            raise ValueError("policy_version must not be blank")
        if not self.entries:
            raise ValueError("policy must contain entries")

        by_exchange: dict[str, EnabledExchange] = {}

        for entry in self.entries:
            if not isinstance(entry, EnabledExchange):
                raise TypeError(
                    "policy entries must be EnabledExchange"
                )
            if entry.exchange_code in by_exchange:
                raise ValueError(
                    "duplicate policy exchange_code: "
                    f"{entry.exchange_code}"
                )
            by_exchange[entry.exchange_code] = entry

        ordered = tuple(
            sorted(
                by_exchange.values(),
                key=lambda item: item.exchange_code,
            )
        )

        if not any(entry.enabled for entry in ordered):
            raise ValueError(
                "policy must enable at least one exchange"
            )

        object.__setattr__(self, "policy_id", policy_id)
        object.__setattr__(
            self,
            "policy_version",
            policy_version,
        )
        object.__setattr__(self, "entries", ordered)

    @property
    def enabled_entries(
        self,
    ) -> tuple[EnabledExchange, ...]:
        return tuple(
            entry
            for entry in self.entries
            if entry.enabled
        )

    @property
    def enabled_exchange_codes(self) -> tuple[str, ...]:
        return tuple(
            entry.exchange_code
            for entry in self.enabled_entries
        )


def scanner_v1_pilot_policy() -> EnabledExchangePolicy:
    """
    Small live-acceptance policy for S2.1F.

    It is not the final production exchange policy.
    """
    return EnabledExchangePolicy(
        policy_id="scanner-v1-pilot",
        policy_version="1",
        entries=(
            EnabledExchange(
                exchange_code="LU",
                provider_id="eodhd-exchange-symbols",
                coverage_scope=(
                    ExchangeCoverageScope.FULL_EXCHANGE
                ),
            ),
            EnabledExchange(
                exchange_code="BIT",
                provider_id="borsa-italiana-ftse-mib",
                coverage_scope=(
                    ExchangeCoverageScope.INDEX_FALLBACK
                ),
            ),
        ),
    )
