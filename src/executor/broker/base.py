"""BrokerClient protocol — broker-agnostic surface used by `execute.py`.

Keep this minimal: only the calls the executor actually needs. Anything richer
belongs in the concrete implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from ..signal import Action


@dataclass
class OrderHandle:
    broker_id: str
    ticker: str
    action: Action
    qty: float
    limit_price: float


@dataclass
class OrderResult:
    status: str  # "filled" | "partial" | "cancelled" | "rejected" | "timeout"
    filled_qty: float
    avg_fill_price: float | None
    raw_status: str  # broker's native status string, for receipt


@dataclass(frozen=True)
class OptionContract:
    """A US equity option, 100-share multiplier, SMART-routed."""

    symbol: str
    expiry: str  # YYYYMMDD
    strike: float
    right: str  # "C" | "P"

    @property
    def label(self) -> str:
        return f"{self.symbol} {self.expiry} {self.strike:g}{self.right}"

    @property
    def expiry_date(self) -> date:
        return datetime.strptime(self.expiry, "%Y%m%d").date()

    def dte(self, today: date) -> int:
        return (self.expiry_date - today).days


@dataclass
class OptionQuote:
    bid: float | None
    ask: float | None
    last: float | None
    close: float | None
    delta: float | None = None
    iv: float | None = None
    underlying: float | None = None

    def reference(self) -> tuple[float | None, str]:
        """Best fair-value estimate: mid if there is a two-sided market, else last, else close."""
        if self.bid and self.ask:
            return round((self.bid + self.ask) / 2, 4), "mid"
        if self.last:
            return self.last, "last"
        if self.close:
            return self.close, "close"
        return None, "none"


@dataclass
class OpenOptionOrder:
    contract: OptionContract
    action: Action
    qty: float


class BrokerClient(Protocol):
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def get_buying_power(self) -> float: ...
    async def get_cash(self) -> float: ...
    async def get_free_cash(self) -> float: ...
    async def get_position(self, ticker: str) -> float: ...
    async def get_open_orders(self, ticker: str) -> list[OrderHandle]: ...
    async def get_quote(self, ticker: str) -> float: ...
    async def place_limit_order(
        self, ticker: str, action: Action, qty: float, limit_price: float
    ) -> OrderHandle: ...
    async def poll_order(self, handle: OrderHandle, timeout_s: int) -> OrderResult: ...
    async def cancel_order(self, handle: OrderHandle) -> None: ...


class OptionsBroker(BrokerClient, Protocol):
    async def get_option_chain(self, symbol: str) -> tuple[list[str], list[float]]: ...
    async def get_option_strikes(self, symbol: str, expiry: str) -> list[float]: ...
    async def get_option_quote(self, contract: OptionContract) -> OptionQuote: ...
    async def get_option_positions(self) -> dict[OptionContract, float]: ...
    async def get_open_option_orders(self) -> list[OpenOptionOrder]: ...
    async def get_committed_put_collateral(self) -> float: ...
    async def place_option_limit_order(
        self, contract: OptionContract, action: Action, qty: int, limit_price: float, tif: str
    ) -> OrderHandle: ...
