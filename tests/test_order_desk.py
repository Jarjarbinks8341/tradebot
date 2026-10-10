from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date

import pytest

from executor.broker.base import OptionContract, OptionQuote, OrderHandle
from executor.config import Settings
from executor.order_desk import OrderDesk, OrderRejected
from executor.signal import Action
from executor.store import Store


class FakeBroker:
    def __init__(
        self,
        quote=85.7,
        cash=10_100.0,
        position=0.0,
        open_orders=None,
        option_quote=None,
        option_positions=None,
        committed_collateral=0.0,
    ):
        self.quote = quote
        self.cash = cash
        self.position = position
        self.open_orders = open_orders or []
        self.option_quote = option_quote or OptionQuote(bid=1.10, ask=1.20, last=1.15, close=1.12)
        self.option_positions = option_positions or {}
        self.committed_collateral = committed_collateral
        self.placed: list[tuple] = []

    async def connect(self): ...
    async def disconnect(self): ...
    async def get_quote(self, ticker): return self.quote
    async def get_cash(self): return self.cash
    async def get_free_cash(self): return self.cash - self.committed_collateral
    async def get_committed_put_collateral(self): return self.committed_collateral
    async def get_option_quote(self, contract): return self.option_quote
    async def get_option_positions(self): return self.option_positions

    async def place_option_limit_order(self, contract, action, qty, limit_price, tif="DAY"):
        self.placed.append((contract, action, qty, limit_price, tif))
        return OrderHandle(str(len(self.placed)), contract.label, action, qty, limit_price)
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
        max_put_collateral_usd=9000.0,
        min_long_call_dte=180,
        max_option_limit_from_mid_pct=10.0,
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


def test_stock_buy_excludes_cash_securing_puts(settings):
    broker = FakeBroker(cash=10_100.0, committed_collateral=9_800.0)
    with pytest.raises(OrderRejected, match="cash"):
        run(_desk(settings, broker).preview("KO", "BUY", 10, 85.85))


# --- options ---------------------------------------------------------------

TODAY = date(2026, 10, 12)
KO_PUT = OptionContract("KO", "20261120", 85.0, "P")
KO_LEAP = OptionContract("KO", "20271217", 90.0, "C")


@pytest.fixture
def osettings(settings):
    # One KO put secures $8,500 — above the stock-sized $5k daily cap used above.
    return replace(settings, max_daily_usd=10_000.0)


def opt(desk, **kw):
    args = {
        "ticker": "KO", "expiry": "2026-11-20", "strike": 85.0, "right": "P",
        "side": "SELL", "qty": 1, "limit_price": 1.15, "today": TODAY,
    } | kw
    return run(desk.preview_option(**args))


def test_sell_cash_secured_put_then_place(osettings):
    broker = FakeBroker()
    store = Store(osettings.state_db)
    desk = OrderDesk(osettings, store, lambda: broker)
    p = opt(desk)
    assert p["contract"] == "KO 20261120 85P"
    assert p["strategy"] == "sell cash-secured put (open)"
    assert p["premium_usd"] == pytest.approx(115.0)
    assert p["assignment_cash_usd"] == pytest.approx(8500.0)
    assert p["breakeven"] == pytest.approx(83.85)
    assert p["days_to_expiry"] == 39
    r = run(desk.place(p["preview_id"]))
    assert broker.placed == [(KO_PUT, Action.SELL, 1, 1.15, "DAY")]
    assert r["contract"] == "KO 20261120 85P"
    from datetime import UTC, datetime, timedelta

    # put collateral, not premium, counts toward the daily cap
    assert store.placed_buy_notional_since(datetime.now(UTC) - timedelta(hours=1)) == 8500.0


def test_put_must_be_fully_cash_secured(osettings):
    with pytest.raises(OrderRejected, match="cash-secured"):
        opt(_desk(osettings, FakeBroker(cash=8_000.0)))


def test_existing_short_puts_reduce_free_cash(osettings):
    broker = FakeBroker(cash=10_100.0, committed_collateral=5_000.0)
    with pytest.raises(OrderRejected, match="cash-secured"):
        opt(_desk(osettings, broker))


def test_put_collateral_cap(osettings):
    with pytest.raises(OrderRejected, match="put_collateral_within_cap"):
        opt(_desk(osettings, FakeBroker(cash=50_000.0)), strike=95.0)


def test_put_collateral_counts_toward_daily_cap(osettings):
    settings = replace(osettings, max_daily_usd=8_000.0)
    with pytest.raises(OrderRejected, match="daily"):
        opt(_desk(settings, FakeBroker()))


def test_buy_leaps_call(osettings):
    broker = FakeBroker(option_quote=OptionQuote(bid=6.80, ask=7.20, last=7.0, close=6.9))
    desk = _desk(osettings, broker)
    p = opt(desk, expiry="20271217", strike=90.0, right="CALL", side="BUY", limit_price=7.0)
    assert p["strategy"] == "buy call (open)"
    assert p["max_loss_usd"] == pytest.approx(700.0)
    assert p["breakeven"] == pytest.approx(97.0)
    run(desk.place(p["preview_id"]))
    assert broker.placed == [(KO_LEAP, Action.BUY, 1, 7.0, "DAY")]


def test_short_dated_call_is_not_a_leap(osettings):
    with pytest.raises(OrderRejected, match="days to expiry"):
        opt(_desk(osettings, FakeBroker()), right="C", side="BUY")


def test_call_premium_respects_order_cap_and_free_cash(osettings):
    q = OptionQuote(bid=6.80, ask=7.20, last=7.0, close=6.9)
    leap = {"expiry": "20271217", "strike": 90.0, "right": "C", "side": "BUY", "limit_price": 7.0}
    with pytest.raises(OrderRejected, match="per-order cap"):
        opt(_desk(osettings, FakeBroker(option_quote=q)), qty=4, **leap)
    broker = FakeBroker(option_quote=q, committed_collateral=9_500.0)
    with pytest.raises(OrderRejected, match="cash"):
        opt(_desk(osettings, broker), **leap)


def test_naked_call_refused(osettings):
    with pytest.raises(OrderRejected, match="long_call_held"):
        opt(_desk(osettings, FakeBroker()), right="C", side="SELL")


def test_sell_to_close_long_call(osettings):
    broker = FakeBroker(option_positions={KO_LEAP: 2.0},
                        option_quote=OptionQuote(bid=6.80, ask=7.20, last=7.0, close=6.9))
    p = opt(_desk(osettings, broker), expiry="20271217", strike=90.0, right="C",
            side="SELL", qty=2, limit_price=7.0)
    assert p["strategy"] == "sell long call (close)"


def test_long_put_refused_but_buy_to_close_allowed(osettings):
    with pytest.raises(OrderRejected, match="short_put_held"):
        opt(_desk(osettings, FakeBroker()), side="BUY")
    broker = FakeBroker(option_positions={KO_PUT: -1.0})
    p = opt(_desk(osettings, broker), side="BUY")
    assert p["strategy"] == "buy back short put (close)"


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"limit_price": 0.50}, "from reference"),
        ({"qty": 1.5}, "whole number of contracts"),
        ({"right": "X"}, "right"),
        ({"expiry": "Nov 20"}, "expiry"),
        ({"expiry": "2026-10-01"}, "days to expiry"),
    ],
)
def test_option_preview_rejects_bad_orders(osettings, kwargs, match):
    with pytest.raises(OrderRejected, match=match):
        opt(_desk(osettings, FakeBroker()), **kwargs)


def test_option_limit_falls_back_to_close_when_market_shut(osettings):
    broker = FakeBroker(option_quote=OptionQuote(bid=None, ask=None, last=None, close=1.12))
    p = opt(_desk(osettings, broker))
    assert p["quote"]["reference_source"] == "close"
