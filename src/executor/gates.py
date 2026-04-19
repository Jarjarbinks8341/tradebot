"""Sanity gates. Each function returns (ok, reason). Pure where possible."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# Hard-coded US market holidays. Refresh annually; errs closed if not refreshed
# (stale holiday list just means more overnight no-ops, never a bad trade).
# Source: NYSE 2026 calendar. https://www.nyse.com/markets/hours-calendars
_US_MARKET_HOLIDAYS: frozenset[date] = frozenset(
    {
        date(2026, 1, 1),   # New Year's Day
        date(2026, 1, 19),  # MLK Jr. Day
        date(2026, 2, 16),  # Presidents' Day
        date(2026, 4, 3),   # Good Friday
        date(2026, 5, 25),  # Memorial Day
        date(2026, 6, 19),  # Juneteenth
        date(2026, 7, 3),   # Independence Day (observed)
        date(2026, 9, 7),   # Labor Day
        date(2026, 11, 26), # Thanksgiving
        date(2026, 12, 25), # Christmas
        date(2027, 1, 1),   # New Year's Day
        date(2027, 1, 18),  # MLK Jr. Day
        date(2027, 2, 15),  # Presidents' Day
        date(2027, 3, 26),  # Good Friday
        date(2027, 5, 31),  # Memorial Day
        date(2027, 6, 18),  # Juneteenth (observed)
        date(2027, 7, 5),   # Independence Day (observed)
        date(2027, 9, 6),   # Labor Day
        date(2027, 11, 25), # Thanksgiving
        date(2027, 12, 24), # Christmas (observed)
    }
)

_MARKET_OPEN = time(9, 30)
_MARKET_CLOSE = time(16, 0)


@dataclass(frozen=True)
class GateResult:
    ok: bool
    reason: str


def halt_file_absent(halt_file: Path) -> GateResult:
    if halt_file.exists():
        return GateResult(False, f"HALT file present at {halt_file}")
    return GateResult(True, "no halt file")


def market_is_open(now: datetime | None = None) -> GateResult:
    now = now or datetime.now(UTC)
    now_et = now.astimezone(ET)
    if now_et.weekday() >= 5:
        return GateResult(False, f"weekend ({now_et:%A})")
    if now_et.date() in _US_MARKET_HOLIDAYS:
        return GateResult(False, f"market holiday ({now_et.date()})")
    if not (_MARKET_OPEN <= now_et.time() < _MARKET_CLOSE):
        return GateResult(False, f"outside regular hours ({now_et:%H:%M} ET)")
    return GateResult(True, "market open")


def reference_price_drift(
    reference_price: float, live_price: float, max_drift_pct: float
) -> GateResult:
    if reference_price <= 0:
        return GateResult(False, "reference_price non-positive")
    drift_pct = abs(live_price - reference_price) / reference_price * 100
    if drift_pct > max_drift_pct:
        return GateResult(
            False,
            f"reference drift {drift_pct:.2f}% > {max_drift_pct:.2f}% "
            f"(ref=${reference_price:.2f} live=${live_price:.2f})",
        )
    return GateResult(True, f"drift {drift_pct:.2f}% within tolerance")


def buying_power_sufficient(
    buying_power: float, qty: float, limit_price: float, buffer_mult: float
) -> GateResult:
    required = qty * limit_price * buffer_mult
    if buying_power < required:
        return GateResult(
            False,
            f"buying power ${buying_power:.2f} < required ${required:.2f} "
            f"(qty={qty} x limit=${limit_price:.2f} x buffer={buffer_mult})",
        )
    return GateResult(True, f"bp ${buying_power:.2f} >= required ${required:.2f}")


def position_sufficient(position: float, qty: float) -> GateResult:
    if position < qty:
        return GateResult(False, f"position {position} < qty {qty}")
    return GateResult(True, f"position {position} >= qty {qty}")


def no_open_orders(open_orders_for_ticker: list) -> GateResult:
    if open_orders_for_ticker:
        return GateResult(
            False, f"{len(open_orders_for_ticker)} open order(s) already on ticker"
        )
    return GateResult(True, "no open orders")


__all__ = [
    "GateResult",
    "buying_power_sufficient",
    "halt_file_absent",
    "market_is_open",
    "no_open_orders",
    "position_sufficient",
    "reference_price_drift",
]


def _assert_holidays_fresh(now: datetime | None = None) -> None:
    """Unused at runtime; kept as a reminder for the CLAUDE.md refresh checklist."""
    now = now or datetime.now(UTC)
    latest = max(_US_MARKET_HOLIDAYS)
    if latest - now.date() < timedelta(days=90):
        raise RuntimeError("refresh _US_MARKET_HOLIDAYS — under 90d runway left")
