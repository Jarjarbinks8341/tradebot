from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from executor.broker.base import OrderHandle
from executor.config import Settings
from executor.order_desk import OrderDesk, OrderRejected
from executor.signal import Action
from executor.store import Store


class FakeBroker:
    def __init__(self, quote=85.7, cash=10_100.0, position=0.0, open_orders=None):
        self.quote = quote
        self.cash = cash
        self.position = position
        self.open_orders = open_orders or []
        self.placed: list[tuple] = []

    async def connect(self): ...
    async def disconnect(self): ...
    async def get_quote(self, ticker): return self.quote
    async def get_cash(self): return self.cash
    async def get_position(self, ticker): return self.position
    async def get_open_orders(self, ticker): return self.open_orders

    async def place_limit_order(self, ticker, action, qty, limit_price, tif="DAY"):
        self.placed.append((ticker, action, qty, limit_price, tif))
        return OrderHandle(str(len(self.placed)), ticker, action, qty, limit_price)


@pytest.fixture
def settings(tmp_path):
    s = Settings.load(tmp_path)
    return replace(
        s,
        state_db=tmp_path / "s.db",
        halt_file=tmp_path / "HALT",
        max_order_usd=2500.0,
        max_daily_usd=5000.0,
        max_limit_from_quote_pct=3.0,
        buying_power_buffer=1.02,
    )


def _desk(settings, broker):
    return OrderDesk(settings, Store(settings.state_db), lambda: broker)


def run(coro):
    return asyncio.run(coro)


def test_preview_then_place_buys(settings):
    broker = FakeBroker()
    desk = _desk(settings, broker)
    p = run(desk.preview("ko", "BUY", 10, 85.85))
    assert p["ticker"] == "KO"
    assert p["notional_usd"] == pytest.approx(858.5)
    r = run(desk.place(p["preview_id"]))
    assert r["order_id"] == "1"
    assert broker.placed == [("KO", Action.BUY, 10, 85.85, "DAY")]


def test_place_without_preview_is_refused(settings):
    desk = _desk(settings, FakeBroker())
    with pytest.raises(OrderRejected, match="preview"):
        run(desk.place("made-up-id"))


def test_preview_is_single_use(settings):
    broker = FakeBroker()
    desk = _desk(settings, broker)
    p = run(desk.preview("KO", "BUY", 10, 85.85))
    run(desk.place(p["preview_id"]))
    with pytest.raises(OrderRejected):
        run(desk.place(p["preview_id"]))
    assert len(broker.placed) == 1


def test_expired_preview_is_refused(settings):
    desk = _desk(settings, FakeBroker())
    desk.preview_ttl_s = -1
    p = run(desk.preview("KO", "BUY", 10, 85.85))
    with pytest.raises(OrderRejected, match="expired"):
        run(desk.place(p["preview_id"]))


def test_halt_file_blocks_place_even_after_preview(settings):
    broker = FakeBroker()
    desk = _desk(settings, broker)
    p = run(desk.preview("KO", "BUY", 10, 85.85))
    settings.halt_file.write_text("")
    with pytest.raises(OrderRejected, match="HALT"):
        run(desk.place(p["preview_id"]))
    assert broker.placed == []


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"qty": 1.5}, "whole"),
        ({"limit_price": 95.0}, "from quote"),
        ({"qty": 40}, "per-order cap"),
        ({"side": "SHORT"}, "side"),
    ],
)
def test_preview_rejects_bad_orders(settings, kwargs, match):
    args = {"ticker": "KO", "side": "BUY", "qty": 10, "limit_price": 85.85} | kwargs
    with pytest.raises(OrderRejected, match=match):
        run(_desk(settings, FakeBroker()).preview(**args))


def test_buy_never_uses_margin(settings):
    with pytest.raises(OrderRejected, match="cash"):
        run(_desk(settings, FakeBroker(cash=500.0)).preview("KO", "BUY", 10, 85.85))


def test_sell_requires_position(settings):
    with pytest.raises(OrderRejected, match="position"):
        run(_desk(settings, FakeBroker(position=3)).preview("KO", "SELL", 10, 85.6))
    p = run(_desk(settings, FakeBroker(position=10)).preview("KO", "SELL", 10, 85.6))
    assert p["side"] == "SELL"


def test_daily_cap_counts_executor_and_mcp_orders(settings):
    store = Store(settings.state_db)
    store.record_order("executor", "TQQQ", "BUY", 50, 90.0)  # $4,500 already today
    desk = OrderDesk(settings, store, lambda: FakeBroker())
    with pytest.raises(OrderRejected, match="daily"):
        run(desk.preview("KO", "BUY", 10, 85.85))


def test_placed_order_is_recorded_in_ledger(settings):
    store = Store(settings.state_db)
    desk = OrderDesk(settings, store, lambda: FakeBroker())
    p = run(desk.preview("KO", "BUY", 10, 85.85))
    run(desk.place(p["preview_id"]))
    from datetime import UTC, datetime, timedelta

    assert store.placed_buy_notional_since(datetime.now(UTC) - timedelta(hours=1)) == 858.5
