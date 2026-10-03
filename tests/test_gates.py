from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from executor import gates

ET = ZoneInfo("America/New_York")


def test_halt_file_absent_ok(tmp_path):
    assert gates.halt_file_absent(tmp_path / "HALT").ok


def test_halt_file_present_blocks(tmp_path):
    halt = tmp_path / "HALT"
    halt.write_text("")
    g = gates.halt_file_absent(halt)
    assert not g.ok
    assert "HALT" in g.reason


def test_market_open_weekday_midday():
    now = datetime(2026, 4, 20, 11, 0, tzinfo=ET)  # Mon 11:00 ET
    assert gates.market_is_open(now).ok


def test_market_closed_weekend():
    now = datetime(2026, 4, 18, 11, 0, tzinfo=ET)  # Sat
    g = gates.market_is_open(now)
    assert not g.ok
    assert "weekend" in g.reason.lower()


def test_market_closed_before_open():
    now = datetime(2026, 4, 20, 8, 0, tzinfo=ET)  # Mon 08:00 ET
    g = gates.market_is_open(now)
    assert not g.ok


def test_market_closed_after_close():
    now = datetime(2026, 4, 20, 17, 0, tzinfo=ET)  # Mon 17:00 ET
    g = gates.market_is_open(now)
    assert not g.ok


def test_market_closed_on_holiday():
    now = datetime(2026, 12, 25, 11, 0, tzinfo=ET)  # Christmas
    g = gates.market_is_open(now)
    assert not g.ok
    assert "holiday" in g.reason.lower()


def test_drift_within_tolerance():
    assert gates.reference_price_drift(100.0, 104.0, 5.0).ok


def test_drift_exceeds_tolerance():
    g = gates.reference_price_drift(100.0, 106.0, 5.0)
    assert not g.ok
    assert "drift" in g.reason


def test_buying_power_ok():
    assert gates.buying_power_sufficient(1000.0, 10, 50.0, 1.02).ok


def test_buying_power_short():
    g = gates.buying_power_sufficient(100.0, 10, 50.0, 1.02)
    assert not g.ok
    assert "buying power" in g.reason.lower()


def test_position_sufficient():
    assert gates.position_sufficient(10.0, 5.0).ok


def test_position_insufficient():
    g = gates.position_sufficient(3.0, 5.0)
    assert not g.ok


def test_no_open_orders_empty_list():
    assert gates.no_open_orders([]).ok


def test_no_open_orders_blocks_when_present():
    g = gates.no_open_orders(["placeholder"])
    assert not g.ok


# --- Live-trading guards (no margin, hard notional caps) ---


def test_cash_sufficient_ok():
    assert gates.cash_sufficient(1000.0, 10, 50.0, 1.02).ok


def test_cash_sufficient_rejects_margin_funded_buy():
    # $10,100 cash, $67k buying power — a $20k buy must be rejected on cash alone.
    g = gates.cash_sufficient(10_100.0, 400, 50.0, 1.02)
    assert not g.ok
    assert "cash" in g.reason.lower()


def test_order_cap_ok():
    assert gates.order_notional_within_cap(10, 85.0, 2_500.0).ok


def test_order_cap_rejects_oversized():
    g = gates.order_notional_within_cap(35, 100.0, 2_500.0)
    assert not g.ok
    assert "cap" in g.reason.lower()


def test_daily_cap_ok():
    assert gates.daily_notional_within_cap(1_000.0, 2_000.0, 5_000.0).ok


def test_daily_cap_rejects_when_total_would_exceed():
    g = gates.daily_notional_within_cap(4_000.0, 1_500.0, 5_000.0)
    assert not g.ok
    assert "daily" in g.reason.lower()


def test_limit_near_quote_ok():
    assert gates.limit_near_quote(86.0, 85.7, 3.0).ok


def test_limit_near_quote_rejects_fat_finger():
    # Typo 857 instead of 85.7 must never reach the exchange.
    g = gates.limit_near_quote(857.0, 85.7, 3.0)
    assert not g.ok


def test_whole_shares_rejects_fraction():
    assert not gates.whole_shares(1.5).ok
    assert not gates.whole_shares(0).ok
    assert gates.whole_shares(10).ok
