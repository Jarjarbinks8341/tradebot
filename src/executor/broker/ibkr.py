"""IBKR broker implementation via `ib-async`.

Connects to a local IB Gateway session (launchd-managed on the Mac mini).
Rate-limit hygiene: one `IB()` per `execute.py` invocation; account summary +
positions are cached on first read for the lifetime of the connection.
"""

from __future__ import annotations

import asyncio
import logging
import math

from ib_async import IB, LimitOrder, Option, Stock

from ..signal import Action
from .base import (
    OpenOptionOrder,
    OptionContract,
    OptionQuote,
    OptionsBroker,
    OrderHandle,
    OrderResult,
)

log = logging.getLogger(__name__)


def _num(x: float | None) -> float | None:
    """ib_async reports missing ticks as nan or -1; normalise to None."""
    if x is None or (isinstance(x, float) and math.isnan(x)) or x <= 0:
        return None
    return float(x)


def _ib_option(c: OptionContract) -> Option:
    return Option(c.symbol, c.expiry, c.strike, c.right, "SMART", multiplier="100", currency="USD")


def _option_contract(ib_contract) -> OptionContract:
    return OptionContract(
        symbol=ib_contract.symbol,
        expiry=ib_contract.lastTradeDateOrContractMonth,
        strike=float(ib_contract.strike),
        right=ib_contract.right[0],
    )


class IBKRBroker(OptionsBroker):
    def __init__(self, host: str, port: int, client_id: int) -> None:
        self.host = host
        self.port = port
        self.client_id = client_id
        self.ib = IB()
        self._cached_bp: float | None = None
        self._cached_cash: float | None = None
        self._cached_positions: list | None = None

    async def connect(self) -> None:
        await self.ib.connectAsync(self.host, self.port, clientId=self.client_id)
        log.info(
            "connected to IB Gateway %s:%s (clientId=%s, accounts=%s)",
            self.host,
            self.port,
            self.client_id,
            self.ib.managedAccounts(),
        )

    async def disconnect(self) -> None:
        if self.ib.isConnected():
            self.ib.disconnect()

    async def get_buying_power(self) -> float:
        if self._cached_bp is None:
            summary = await self.ib.accountSummaryAsync()
            bp = next((float(r.value) for r in summary if r.tag == "BuyingPower"), 0.0)
            self._cached_bp = bp
        return self._cached_bp

    async def get_cash(self) -> float:
        """Settled cash (TotalCashValue) — the no-margin spending limit."""
        if self._cached_cash is None:
            summary = await self.ib.accountSummaryAsync()
            self._cached_cash = next(
                (float(r.value) for r in summary if r.tag == "TotalCashValue"), 0.0
            )
        return self._cached_cash

    async def get_free_cash(self) -> float:
        """Settled cash minus collateral already promised to short puts and working put sells."""
        return await self.get_cash() - await self.get_committed_put_collateral()

    async def _positions(self) -> list:
        if self._cached_positions is None:
            self._cached_positions = list(await self.ib.reqPositionsAsync())
        return self._cached_positions

    async def _positions_map(self) -> dict[str, float]:
        """Stock/ETF positions only — option positions share the symbol and must not mix in."""
        return {
            p.contract.symbol: float(p.position)
            for p in await self._positions()
            if p.contract.secType == "STK"
        }

    async def get_option_positions(self) -> dict[OptionContract, float]:
        return {
            _option_contract(p.contract): float(p.position)
            for p in await self._positions()
            if p.contract.secType == "OPT"
        }

    async def get_open_option_orders(self) -> list[OpenOptionOrder]:
        trades = await self.ib.reqAllOpenOrdersAsync()
        return [
            OpenOptionOrder(
                contract=_option_contract(t.contract),
                action=Action.BUY if t.order.action == "BUY" else Action.SELL,
                qty=float(t.order.totalQuantity),
            )
            for t in trades
            if t.contract.secType == "OPT"
        ]

    async def get_committed_put_collateral(self) -> float:
        """Cash that would be needed if every short put (held or working) were assigned."""
        held = sum(
            c.strike * 100 * -qty
            for c, qty in (await self.get_option_positions()).items()
            if c.right == "P" and qty < 0
        )
        working = sum(
            o.contract.strike * 100 * o.qty
            for o in await self.get_open_option_orders()
            if o.contract.right == "P" and o.action == Action.SELL
        )
        return held + working

    async def get_position(self, ticker: str) -> float:
        return (await self._positions_map()).get(ticker.upper(), 0.0)

    async def get_open_orders(self, ticker: str) -> list[OrderHandle]:
        trades = await self.ib.reqAllOpenOrdersAsync()
        handles: list[OrderHandle] = []
        for t in trades:
            if t.contract.symbol != ticker.upper():
                continue
            handles.append(
                OrderHandle(
                    broker_id=str(t.order.orderId),
                    ticker=t.contract.symbol,
                    action=Action.BUY if t.order.action == "BUY" else Action.SELL,
                    qty=float(t.order.totalQuantity),
                    limit_price=float(getattr(t.order, "lmtPrice", 0.0) or 0.0),
                )
            )
        return handles

    async def get_quote(self, ticker: str) -> float:
        contract = Stock(ticker.upper(), "SMART", "USD")
        await self.ib.qualifyContractsAsync(contract)
        # Fall back to delayed data (type 3) for paper accounts without live subscriptions.
        self.ib.reqMarketDataType(3)
        ticker_obj = self.ib.reqMktData(contract, "", False, False)
        for _ in range(40):
            await asyncio.sleep(0.25)
            price = (
                ticker_obj.last
                or ticker_obj.close
                or getattr(ticker_obj, "delayedLast", None)
                or getattr(ticker_obj, "delayedClose", None)
                or ticker_obj.marketPrice()
            )
            if price and price > 0:
                self.ib.cancelMktData(contract)
                return float(price)
        self.ib.cancelMktData(contract)
        raise RuntimeError(f"no quote available for {ticker}")

    async def get_option_chain(self, symbol: str) -> tuple[list[str], list[float]]:
        stock = Stock(symbol.upper(), "SMART", "USD")
        await self.ib.qualifyContractsAsync(stock)
        if not stock.conId:
            raise ValueError(f"unknown stock {symbol}")
        params = await self.ib.reqSecDefOptParamsAsync(stock.symbol, "", "STK", stock.conId)
        chain = next((p for p in params if p.exchange == "SMART"), None)
        if chain is None:
            raise ValueError(f"no listed options for {symbol}")
        return sorted(chain.expirations), sorted(chain.strikes)

    async def get_option_strikes(self, symbol: str, expiry: str) -> list[float]:
        """Strikes actually listed for one expiry (the chain's strike list is a union)."""
        probe = Option(symbol.upper(), expiry, exchange="SMART", currency="USD", right="C")
        details = await self.ib.reqContractDetailsAsync(probe)
        return sorted({d.contract.strike for d in details})

    async def _qualified_option(self, c: OptionContract) -> Option:
        contract = _ib_option(c)
        await self.ib.qualifyContractsAsync(contract)
        if not contract.conId:
            raise ValueError(f"no such option {c.label} — check expiry/strike with the chain")
        return contract

    async def get_option_quote(self, c: OptionContract) -> OptionQuote:
        contract = await self._qualified_option(c)
        self.ib.reqMarketDataType(3)  # live if subscribed, else delayed / frozen
        t = self.ib.reqMktData(contract, "", False, False)
        try:
            for _ in range(40):
                await asyncio.sleep(0.25)
                if _num(t.bid) and _num(t.ask) and t.modelGreeks:
                    break
        finally:
            self.ib.cancelMktData(contract)
        greeks = t.modelGreeks
        q = OptionQuote(
            bid=_num(t.bid),
            ask=_num(t.ask),
            last=_num(t.last),
            close=_num(t.close),
            delta=greeks.delta if greeks and greeks.delta is not None else None,
            iv=_num(greeks.impliedVol) if greeks else None,
            underlying=_num(greeks.undPrice) if greeks else None,
        )
        if q.reference()[0] is None:
            raise RuntimeError(f"no quote available for {c.label}")
        return q

    async def place_option_limit_order(
        self, c: OptionContract, action: Action, qty: int, limit_price: float, tif: str = "DAY"
    ) -> OrderHandle:
        contract = await self._qualified_option(c)
        order = LimitOrder(action.value, int(qty), float(limit_price), tif=tif)
        trade = self.ib.placeOrder(contract, order)
        self._cached_bp = None
        self._cached_cash = None
        self._cached_positions = None
        return OrderHandle(
            broker_id=str(trade.order.orderId),
            ticker=c.label,
            action=action,
            qty=float(qty),
            limit_price=float(limit_price),
        )

    async def place_limit_order(
        self, ticker: str, action: Action, qty: float, limit_price: float, tif: str = "DAY"
    ) -> OrderHandle:
        if qty != int(qty):
            raise ValueError(
                f"fractional qty {qty} not supported in Phase 1 — "
                "producer should emit integer qty_shares"
            )
        contract = Stock(ticker.upper(), "SMART", "USD")
        await self.ib.qualifyContractsAsync(contract)
        order = LimitOrder(action.value, int(qty), float(limit_price), tif=tif)
        trade = self.ib.placeOrder(contract, order)
        # Invalidate caches — bp and positions will change.
        self._cached_bp = None
        self._cached_cash = None
        self._cached_positions = None
        return OrderHandle(
            broker_id=str(trade.order.orderId),
            ticker=ticker.upper(),
            action=action,
            qty=float(qty),
            limit_price=float(limit_price),
        )

    async def poll_order(self, handle: OrderHandle, timeout_s: int) -> OrderResult:
        deadline = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < deadline:
            trades = self.ib.trades()
            trade = next((t for t in trades if str(t.order.orderId) == handle.broker_id), None)
            if trade is None:
                await asyncio.sleep(1.0)
                continue
            status = trade.orderStatus.status
            if status in ("Filled",):
                return OrderResult(
                    status="filled",
                    filled_qty=float(trade.orderStatus.filled),
                    avg_fill_price=float(trade.orderStatus.avgFillPrice)
                    if trade.orderStatus.avgFillPrice
                    else None,
                    raw_status=status,
                )
            if status in ("Cancelled", "ApiCancelled"):
                return OrderResult(
                    status="cancelled",
                    filled_qty=float(trade.orderStatus.filled),
                    avg_fill_price=None,
                    raw_status=status,
                )
            if status in ("Inactive",) or status.startswith("Reject"):
                return OrderResult(
                    status="rejected",
                    filled_qty=0.0,
                    avg_fill_price=None,
                    raw_status=status,
                )
            await asyncio.sleep(2.0)
        return OrderResult(
            status="timeout", filled_qty=0.0, avg_fill_price=None, raw_status="timeout"
        )

    async def cancel_order(self, handle: OrderHandle) -> None:
        trades = self.ib.trades()
        trade = next((t for t in trades if str(t.order.orderId) == handle.broker_id), None)
        if trade is not None:
            self.ib.cancelOrder(trade.order)
