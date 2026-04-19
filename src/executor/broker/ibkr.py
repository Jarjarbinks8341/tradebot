"""IBKR broker implementation via `ib-async`.

Connects to a local IB Gateway session (launchd-managed on the Mac mini).
Rate-limit hygiene: one `IB()` per `execute.py` invocation; account summary +
positions are cached on first read for the lifetime of the connection.
"""

from __future__ import annotations

import asyncio
import logging

from ib_async import IB, LimitOrder, Stock

from ..signal import Action
from .base import BrokerClient, OrderHandle, OrderResult

log = logging.getLogger(__name__)


class IBKRBroker(BrokerClient):
    def __init__(self, host: str, port: int, client_id: int) -> None:
        self.host = host
        self.port = port
        self.client_id = client_id
        self.ib = IB()
        self._cached_bp: float | None = None
        self._cached_positions: dict[str, float] | None = None

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

    async def _positions_map(self) -> dict[str, float]:
        if self._cached_positions is None:
            positions = await self.ib.reqPositionsAsync()
            self._cached_positions = {p.contract.symbol: float(p.position) for p in positions}
        return self._cached_positions

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
        ticker_obj = self.ib.reqMktData(contract, "", False, False)
        for _ in range(20):
            await asyncio.sleep(0.25)
            price = ticker_obj.last or ticker_obj.close or ticker_obj.marketPrice()
            if price and price > 0:
                self.ib.cancelMktData(contract)
                return float(price)
        self.ib.cancelMktData(contract)
        raise RuntimeError(f"no quote available for {ticker}")

    async def place_limit_order(
        self, ticker: str, action: Action, qty: float, limit_price: float
    ) -> OrderHandle:
        if qty != int(qty):
            raise ValueError(
                f"fractional qty {qty} not supported in Phase 1 — "
                "producer should emit integer qty_shares"
            )
        contract = Stock(ticker.upper(), "SMART", "USD")
        await self.ib.qualifyContractsAsync(contract)
        order = LimitOrder(action.value, int(qty), float(limit_price), tif="DAY")
        trade = self.ib.placeOrder(contract, order)
        # Invalidate caches — bp and positions will change.
        self._cached_bp = None
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
