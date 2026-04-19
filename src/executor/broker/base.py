"""BrokerClient protocol — broker-agnostic surface used by `execute.py`.

Keep this minimal: only the calls the executor actually needs. Anything richer
belongs in the concrete implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
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


class BrokerClient(Protocol):
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def get_buying_power(self) -> float: ...
    async def get_position(self, ticker: str) -> float: ...
    async def get_open_orders(self, ticker: str) -> list[OrderHandle]: ...
    async def get_quote(self, ticker: str) -> float: ...
    async def place_limit_order(
        self, ticker: str, action: Action, qty: float, limit_price: float
    ) -> OrderHandle: ...
    async def poll_order(self, handle: OrderHandle, timeout_s: int) -> OrderResult: ...
    async def cancel_order(self, handle: OrderHandle) -> None: ...
