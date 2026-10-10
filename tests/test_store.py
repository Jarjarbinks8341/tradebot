from __future__ import annotations

import pytest

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


def test_store_migrates_old_ledger_without_notional_column(tmp_path):
    import sqlite3
    from datetime import UTC, datetime, timedelta

    db = tmp_path / "old.db"
    with sqlite3.connect(db) as c:
        c.execute(
            "CREATE TABLE placed_orders (placed_at TEXT NOT NULL, source TEXT NOT NULL, "
            "ticker TEXT NOT NULL, side TEXT NOT NULL, qty REAL NOT NULL, "
            "limit_price REAL NOT NULL, order_id TEXT)"
        )
        c.execute(
            "INSERT INTO placed_orders VALUES (?, 'mcp', 'KO', 'BUY', 1, 88.25, '4')",
            (datetime.now(UTC).isoformat(),),
        )
    store = Store(db)
    store.record_order("mcp", "KO 20261120 85P", "SELL", 1, 1.15, notional_usd=8500.0)
    store.record_order("mcp", "KO", "SELL", 5, 90.0)  # stock sells never count
    since = datetime.now(UTC) - timedelta(hours=1)
    assert store.placed_buy_notional_since(since) == pytest.approx(8588.25)
