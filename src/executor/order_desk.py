"""Manual order desk: two-step preview → place, used by the MCP server.

Every order must first be previewed. The preview runs all gates against live
broker data and returns a single-use `preview_id` that expires after
`preview_ttl_s`. `place()` only accepts a valid, unexpired preview — so an
order can never reach the broker without its exact parameters having been
shown first. HALT and the daily cap are re-checked at place time.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from . import gates
from .broker.base import BrokerClient
from .config import Settings
from .signal import Action
from .store import Store

_TIFS = ("DAY", "GTC")


class OrderRejected(Exception):
    pass


@dataclass
class _Preview:
    ticker: str
    action: Action
    qty: int
    limit_price: float
    tif: str
    created: float


class OrderDesk:
    preview_ttl_s: float = 300.0

    def __init__(
        self, settings: Settings, store: Store, broker_factory: Callable[[], BrokerClient]
    ) -> None:
        self.settings = settings
        self.store = store
        self.broker_factory = broker_factory
        self._previews: dict[str, _Preview] = {}

    def _check(self, name: str, g: gates.GateResult, log: list[str]) -> None:
        log.append(f"{name}: {g.reason}")
        if not g.ok:
            raise OrderRejected(f"{name}: {g.reason}")

    def _daily_check(self, notional: float, log: list[str]) -> None:
        since = datetime.now(UTC) - timedelta(hours=24)
        self._check(
            "daily_notional_within_cap",
            gates.daily_notional_within_cap(
                self.store.placed_buy_notional_since(since), notional, self.settings.max_daily_usd
            ),
            log,
        )

    async def preview(
        self, ticker: str, side: str, qty: float, limit_price: float, tif: str = "DAY"
    ) -> dict:
        s = self.settings
        log: list[str] = []
        ticker = ticker.strip().upper()
        side = side.strip().upper()
        tif = tif.strip().upper()
        if side not in ("BUY", "SELL"):
            raise OrderRejected(f"side must be BUY or SELL, got {side!r}")
        if tif not in _TIFS:
            raise OrderRejected(f"tif must be one of {_TIFS}, got {tif!r}")
        action = Action(side)

        self._check("halt_file_absent", gates.halt_file_absent(s.halt_file), log)
        self._check("whole_shares", gates.whole_shares(qty), log)
        notional = qty * limit_price
        if action == Action.BUY:
            self._check(
                "order_notional_within_cap",
                gates.order_notional_within_cap(qty, limit_price, s.max_order_usd),
                log,
            )
            self._daily_check(notional, log)

        broker = self.broker_factory()
        await broker.connect()
        try:
            quote = await broker.get_quote(ticker)
            self._check(
                "limit_near_quote",
                gates.limit_near_quote(limit_price, quote, s.max_limit_from_quote_pct),
                log,
            )
            self._check(
                "no_open_orders", gates.no_open_orders(await broker.get_open_orders(ticker)), log
            )
            if action == Action.BUY:
                cash = await broker.get_cash()
                self._check(
                    "cash_sufficient",
                    gates.cash_sufficient(cash, qty, limit_price, s.buying_power_buffer),
                    log,
                )
            else:
                position = await broker.get_position(ticker)
                self._check("position_sufficient", gates.position_sufficient(position, qty), log)
        finally:
            await broker.disconnect()

        preview_id = secrets.token_hex(4)
        self._previews[preview_id] = _Preview(
            ticker, action, int(qty), float(limit_price), tif, time.monotonic()
        )
        return {
            "preview_id": preview_id,
            "ticker": ticker,
            "side": side,
            "qty": int(qty),
            "limit_price": float(limit_price),
            "tif": tif,
            "notional_usd": round(notional, 2),
            "quote": quote,
            "expires_in_s": self.preview_ttl_s,
            "checks": log,
        }

    async def place(self, preview_id: str) -> dict:
        p = self._previews.pop(preview_id, None)
        if p is None:
            raise OrderRejected("unknown or already-used preview_id — call preview_order first")
        if time.monotonic() - p.created > self.preview_ttl_s:
            raise OrderRejected("preview expired — call preview_order again")

        log: list[str] = []
        self._check("halt_file_absent", gates.halt_file_absent(self.settings.halt_file), log)
        if p.action == Action.BUY:
            self._daily_check(p.qty * p.limit_price, log)

        broker = self.broker_factory()
        await broker.connect()
        try:
            handle = await broker.place_limit_order(
                p.ticker, p.action, p.qty, p.limit_price, tif=p.tif
            )
        finally:
            await broker.disconnect()
        self.store.record_order(
            "mcp", p.ticker, p.action.value, p.qty, p.limit_price, order_id=handle.broker_id
        )
        return {
            "order_id": handle.broker_id,
            "ticker": p.ticker,
            "side": p.action.value,
            "qty": p.qty,
            "limit_price": p.limit_price,
            "tif": p.tif,
        }
