"""SQLite-backed idempotency store.

A signal_id is the stable primary key. Recording a previously-seen id is a
silent upsert — callers rely on `already_executed` for the pre-check, so the
upsert path exists only to tolerate concurrent runs (it shouldn't happen in
practice; launchd fires every hour, each run is brief).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS executed_signals (
    signal_id    TEXT PRIMARY KEY,
    status       TEXT NOT NULL,
    executed_at  TEXT NOT NULL,
    order_id     TEXT,
    fill_price   REAL,
    notes        TEXT
);

CREATE TABLE IF NOT EXISTS placed_orders (
    placed_at    TEXT NOT NULL,
    source       TEXT NOT NULL,   -- "executor" | "mcp"
    ticker       TEXT NOT NULL,
    side         TEXT NOT NULL,
    qty          REAL NOT NULL,
    limit_price  REAL NOT NULL,
    order_id     TEXT
);
"""


class Store:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def already_executed(self, signal_id: str) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM executed_signals WHERE signal_id = ?", (signal_id,)
            ).fetchone()
        return row is not None

    def record(
        self,
        signal_id: str,
        status: str,
        order_id: str | None = None,
        fill_price: float | None = None,
        notes: str | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO executed_signals (
                    signal_id, status, executed_at, order_id, fill_price, notes
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(signal_id) DO UPDATE SET
                    status = excluded.status,
                    executed_at = excluded.executed_at,
                    order_id = COALESCE(excluded.order_id, executed_signals.order_id),
                    fill_price = COALESCE(excluded.fill_price, executed_signals.fill_price),
                    notes = COALESCE(excluded.notes, executed_signals.notes)
                """,
                (signal_id, status, now, order_id, fill_price, notes),
            )

    def record_order(
        self,
        source: str,
        ticker: str,
        side: str,
        qty: float,
        limit_price: float,
        order_id: str | None = None,
    ) -> None:
        """Ledger of every live order sent to the broker, from any source (daily cap input)."""
        with self._conn() as c:
            c.execute(
                "INSERT INTO placed_orders VALUES (?, ?, ?, ?, ?, ?, ?)",
                (datetime.now(UTC).isoformat(), source, ticker, side, qty, limit_price, order_id),
            )

    def placed_buy_notional_since(self, since: datetime) -> float:
        with self._conn() as c:
            row = c.execute(
                "SELECT COALESCE(SUM(qty * limit_price), 0) FROM placed_orders "
                "WHERE side = 'BUY' AND placed_at >= ?",
                (since.astimezone(UTC).isoformat(),),
            ).fetchone()
        return float(row[0])
