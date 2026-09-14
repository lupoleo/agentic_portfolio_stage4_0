from dataclasses import dataclass


@dataclass
class PortfolioPosition:
    name: str
    isin: str
    broker_symbol: str
    market: str
    instrument_type: str
    currency: str

    quantity: float
    average_price: float
    load_exchange_rate: float
    cost_value_eur: float

    market_price: float
    market_exchange_rate: float
    market_value_eur: float

    pnl_percent: float
    pnl_eur: float
    pnl_currency: float

    accrued_interest: float = 0.0

    yahoo_symbol: str | None = None

    @property
    def is_long(self) -> bool:
        return self.quantity > 0

    @property
    def is_short(self) -> bool:
        return self.quantity < 0

    @property
    def direction(self) -> str:
        if self.is_long:
            return "LONG"

        if self.is_short:
            return "SHORT"

        return "FLAT"