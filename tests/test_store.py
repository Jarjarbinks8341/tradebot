from __future__ import annotations

from executor.store import Store


def test_not_executed_initially(tmp_path):
    store = Store(tmp_path / "s.db")
    assert store.already_executed("abc") is False


def test_record_then_already_executed(tmp_path):
    store = Store(tmp_path / "s.db")
    store.record("abc", status="dry-run", notes="fixture")
    assert store.already_executed("abc") is True


def test_re_record_is_idempotent(tmp_path):
    store = Store(tmp_path / "s.db")
    store.record("abc", status="dry-run")
    store.record("abc", status="filled", order_id="42", fill_price=10.5)
    assert store.already_executed("abc") is True


def test_placed_notional_sums_both_sources_since_cutoff(tmp_path):
    from datetime import UTC, datetime, timedelta

    store = Store(tmp_path / "s.db")
    store.record_order("executor", "TQQQ", "BUY", 10, 50.0, order_id="1")
    store.record_order("mcp", "KO", "BUY", 10, 85.0, order_id="2")
    store.record_order("mcp", "KO", "SELL", 5, 85.0, order_id="3")
    since = datetime.now(UTC) - timedelta(hours=1)
    # BUYs only: 500 + 850 — sells free up cash, they don't spend it.
    assert store.placed_buy_notional_since(since) == 1350.0
    assert store.placed_buy_notional_since(datetime.now(UTC) + timedelta(hours=1)) == 0.0
