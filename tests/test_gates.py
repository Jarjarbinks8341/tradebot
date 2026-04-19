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
