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
