"""Manual order desk: two-step preview → place, used by the MCP server.

Every order must first be previewed. The preview runs all gates against live
broker data and returns a single-use `preview_id` that expires after
`preview_ttl_s`. `place()` only accepts a valid, unexpired preview — so an
order can never reach the broker without its exact parameters having been
shown first. HALT and the daily cap are re-checked at place time.

Options are limited to two strategies plus their exits:
  open:  SELL put (fully cash-secured)  |  BUY call (>= MIN_LONG_CALL_DTE days)
  close: BUY put you are short          |  SELL call you hold
Naked calls and long puts are refused. Opening trades count toward the caps —
put collateral (strike x 100) for short puts, premium for long calls.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from . import gates
from .broker.base import OptionContract, OptionsBroker
from .config import Settings
from .gates import ET
from .signal import Action
from .store import Store

_TIFS = ("DAY", "GTC")
_RIGHTS = {"C": "C", "CALL": "C", "P": "P", "PUT": "P"}


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


@dataclass
class _OptionPreview:
    contract: OptionContract
    action: Action
    qty: int
    limit_price: float
    tif: str
    opening: bool
    cap_notional: float  # premium (long call) or collateral (short put); 0 for closes
    created: float


def parse_expiry(expiry: str) -> str:
    raw = expiry.strip().replace("-", "")
    try:
        datetime.strptime(raw, "%Y%m%d")
    except ValueError:
        raise OrderRejected(f"expiry must be YYYY-MM-DD or YYYYMMDD, got {expiry!r}") from None
    return raw


class OrderDesk:
    preview_ttl_s: float = 300.0

    def __init__(
        self, settings: Settings, store: Store, broker_factory: Callable[[], OptionsBroker]
    ) -> None:
        self.settings = settings
        self.store = store
        self.broker_factory = broker_factory
        self._previews: dict[str, _Preview | _OptionPreview] = {}

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
                cash = await broker.get_free_cash()
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

    async def preview_option(
        self,
        ticker: str,
        expiry: str,
        strike: float,
        right: str,
        side: str,
        qty: float,
        limit_price: float,
        tif: str = "DAY",
        today: date | None = None,
    ) -> dict:
        s = self.settings
        log: list[str] = []
        side = side.strip().upper()
        tif = tif.strip().upper()
        r = _RIGHTS.get(right.strip().upper())
        if r is None:
            raise OrderRejected(f"right must be C/CALL or P/PUT, got {right!r}")
        if side not in ("BUY", "SELL"):
            raise OrderRejected(f"side must be BUY or SELL, got {side!r}")
        if tif not in _TIFS:
            raise OrderRejected(f"tif must be one of {_TIFS}, got {tif!r}")
        if strike <= 0:
            raise OrderRejected(f"strike must be positive, got {strike}")
        action = Action(side)
        contract = OptionContract(ticker.strip().upper(), parse_expiry(expiry), float(strike), r)
        today = today or datetime.now(ET).date()
        dte = contract.dte(today)
        # SELL put / BUY call open a position; BUY put / SELL call may only close one.
        opening = (r, action) in (("P", Action.SELL), ("C", Action.BUY))
        strategy = {
            ("P", Action.SELL): "sell cash-secured put (open)",
            ("C", Action.BUY): "buy call (open)",
            ("P", Action.BUY): "buy back short put (close)",
            ("C", Action.SELL): "sell long call (close)",
        }[(r, action)]

        self._check("halt_file_absent", gates.halt_file_absent(s.halt_file), log)
        self._check("whole_contracts", gates.whole_contracts(qty), log)
        qty = int(qty)
        premium = qty * 100 * limit_price
        collateral = qty * 100 * contract.strike if r == "P" else 0.0
        cap_notional = 0.0
        if opening:
            if r == "C":
                self._check(
                    "option_dte_at_least", gates.option_dte_at_least(dte, s.min_long_call_dte), log
                )
                self._check(
                    "order_notional_within_cap",
                    gates.order_notional_within_cap(qty * 100, limit_price, s.max_order_usd),
                    log,
                )
                cap_notional = premium
            else:
                self._check("option_dte_at_least", gates.option_dte_at_least(dte, 1), log)
                self._check(
                    "put_collateral_within_cap",
                    gates.order_notional_within_cap(
                        qty * 100, contract.strike, s.max_put_collateral_usd
                    ),
                    log,
                )
                cap_notional = collateral
            self._daily_check(cap_notional, log)
        else:
            self._check("option_dte_at_least", gates.option_dte_at_least(dte, 0), log)

        broker = self.broker_factory()
        await broker.connect()
        try:
            q = await broker.get_option_quote(contract)
            reference, ref_source = q.reference()
            self._check(
                "limit_reasonable",
                gates.option_limit_reasonable(
                    limit_price, q.bid, q.ask, reference, s.max_option_limit_from_mid_pct
                ),
                log,
            )
            self._check(
                "no_open_orders",
                gates.no_open_orders(await broker.get_open_orders(contract.symbol)),
                log,
            )
            held = (await broker.get_option_positions()).get(contract, 0.0)
            if (r, action) == ("C", Action.BUY):
                self._check(
                    "cash_sufficient",
                    gates.cash_sufficient(
                        await broker.get_free_cash(), qty * 100, limit_price, s.buying_power_buffer
                    ),
                    log,
                )
            elif (r, action) == ("P", Action.SELL):
                self._check(
                    "put_cash_secured",
                    gates.put_cash_secured(
                        await broker.get_cash(),
                        await broker.get_committed_put_collateral(),
                        collateral,
                    ),
                    log,
                )
            elif (r, action) == ("P", Action.BUY):
                self._check("short_put_held", gates.position_sufficient(-held, qty), log)
                self._check(
                    "cash_sufficient",
                    gates.cash_sufficient(
                        await broker.get_cash(), qty * 100, limit_price, s.buying_power_buffer
                    ),
                    log,
                )
            else:
                self._check("long_call_held", gates.position_sufficient(held, qty), log)
        finally:
            await broker.disconnect()

        preview_id = secrets.token_hex(4)
        self._previews[preview_id] = _OptionPreview(
            contract, action, qty, float(limit_price), tif, opening, cap_notional,
            time.monotonic(),
        )
        out = {
            "preview_id": preview_id,
            "contract": contract.label,
            "strategy": strategy,
            "side": side,
            "qty_contracts": qty,
            "limit_price": float(limit_price),
            "tif": tif,
            "days_to_expiry": dte,
            "premium_usd": round(premium, 2),
            "premium_direction": "paid" if action == Action.BUY else "received",
            "quote": {
                "bid": q.bid, "ask": q.ask, "last": q.last, "close": q.close,
                "reference": reference, "reference_source": ref_source,
                "delta": q.delta, "iv": q.iv, "underlying": q.underlying,
            },
            "expires_in_s": self.preview_ttl_s,
            "checks": log,
        }
        if (r, action) == ("P", Action.SELL):
            out["assignment_cash_usd"] = round(collateral, 2)
            out["max_loss_usd"] = round(collateral - premium, 2)
            out["breakeven"] = round(contract.strike - limit_price, 2)
        elif (r, action) == ("C", Action.BUY):
            out["max_loss_usd"] = round(premium, 2)
            out["breakeven"] = round(contract.strike + limit_price, 2)
        return out

    async def place(self, preview_id: str) -> dict:
        p = self._previews.pop(preview_id, None)
        if p is None:
            raise OrderRejected("unknown or already-used preview_id — call preview_order first")
        if time.monotonic() - p.created > self.preview_ttl_s:
            raise OrderRejected("preview expired — call preview_order again")
        if isinstance(p, _OptionPreview):
            return await self._place_option(p)

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

    async def _place_option(self, p: _OptionPreview) -> dict:
        log: list[str] = []
        self._check("halt_file_absent", gates.halt_file_absent(self.settings.halt_file), log)
        if p.opening:
            self._daily_check(p.cap_notional, log)

        broker = self.broker_factory()
        await broker.connect()
        try:
            handle = await broker.place_option_limit_order(
                p.contract, p.action, p.qty, p.limit_price, tif=p.tif
            )
        finally:
            await broker.disconnect()
        self.store.record_order(
            "mcp",
            p.contract.label,
            p.action.value,
            p.qty,
            p.limit_price,
            order_id=handle.broker_id,
            notional_usd=p.cap_notional,
        )
        return {
            "order_id": handle.broker_id,
            "contract": p.contract.label,
            "side": p.action.value,
            "qty_contracts": p.qty,
            "limit_price": p.limit_price,
            "tif": p.tif,
        }
