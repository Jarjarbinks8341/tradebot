"""Entrypoint. Runs one tick: poll, validate, gate, trade (or dry-run), record.

Invoke via `python -m executor.execute` from launchd. Safe to run by hand for
debugging. `--signal-file PATH` short-circuits the GitHub poll and reads a
single JSON file (useful for offline dry-runs against a fixture).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import gates
from .broker.ibkr import IBKRBroker
from .config import Settings
from .poller import FetchedSignal, SignalPoller
from .signal import Action, Signal
from .store import Store

log = logging.getLogger("executor")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )


def _load_signal_from_file(path: Path) -> FetchedSignal:
    raw = json.loads(path.read_text())
    return FetchedSignal(signal=Signal.model_validate(raw), raw=raw, source_sha="local")


def _write_receipt(
    settings: Settings,
    fetched: FetchedSignal,
    status: str,
    gate_log: list[str],
    order_info: dict | None = None,
) -> Path:
    settings.receipts_dir.mkdir(parents=True, exist_ok=True)
    signal = fetched.signal
    date_str = signal.issued_at.date().isoformat()
    path = settings.receipts_dir / f"{date_str}-{signal.signal_id}.json"
    payload = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "status": status,
        "signal": fetched.raw,
        "gate_log": gate_log,
        "order": order_info,
    }
    path.write_text(json.dumps(payload, indent=2, default=str))
    log.info("receipt written: %s", path)
    return path


async def _handle_tradeable(
    settings: Settings,
    store: Store,
    broker: IBKRBroker,
    fetched: FetchedSignal,
    gate_log: list[str],
) -> str:
    """Apply per-signal gates, then place/record. Returns final status."""
    signal = fetched.signal

    quote = await broker.get_quote(signal.ticker)
    g = gates.reference_price_drift(
        signal.reference_price, quote, settings.max_reference_drift_pct
    )
    gate_log.append(f"reference_price_drift: {g.reason}")
    if not g.ok:
        store.record(signal.signal_id, "rejected", notes=g.reason)
        return "rejected"

    open_orders = await broker.get_open_orders(signal.ticker)
    g = gates.no_open_orders(open_orders)
    gate_log.append(f"no_open_orders: {g.reason}")
    if not g.ok:
        store.record(signal.signal_id, "rejected", notes=g.reason)
        return "rejected"

    qty = signal.qty_shares
    if qty is None:
        msg = "signal has no qty_shares (fractional/notional-only not supported in Phase 1)"
        gate_log.append(msg)
        store.record(signal.signal_id, "rejected", notes=msg)
        return "rejected"

    limit = signal.limit_price if signal.limit_price is not None else signal.reference_price
    if signal.action == Action.BUY:
        bp = await broker.get_buying_power()
        g = gates.buying_power_sufficient(bp, qty, limit, settings.buying_power_buffer)
        gate_log.append(f"buying_power_sufficient: {g.reason}")
        if not g.ok:
            store.record(signal.signal_id, "rejected", notes=g.reason)
            return "rejected"
    else:  # SELL
        pos = await broker.get_position(signal.ticker)
        g = gates.position_sufficient(pos, qty)
        gate_log.append(f"position_sufficient: {g.reason}")
        if not g.ok:
            store.record(signal.signal_id, "rejected", notes=g.reason)
            return "rejected"

    if not settings.execute:
        gate_log.append("EXECUTE=false — skipping broker call (dry-run)")
        note = f"would place {signal.action.value} {qty}@{limit}"
        store.record(signal.signal_id, "dry-run", notes=note)
        return "dry-run"

    handle = await broker.place_limit_order(signal.ticker, signal.action, qty, limit)
    log.info(
        "placed %s %s %s @ %s (orderId=%s)",
        signal.action.value,
        qty,
        signal.ticker,
        limit,
        handle.broker_id,
    )
    result = await broker.poll_order(handle, settings.order_poll_timeout_s)
    gate_log.append(
        f"order_result: status={result.status} filled={result.filled_qty} "
        f"avg={result.avg_fill_price}"
    )
    store.record(
        signal.signal_id,
        result.status,
        order_id=handle.broker_id,
        fill_price=result.avg_fill_price,
        notes=result.raw_status,
    )
    if result.status == "timeout":
        await broker.cancel_order(handle)
        gate_log.append("cancelled after poll timeout")
    return result.status


async def _run(settings: Settings, signal_file: Path | None) -> int:
    store = Store(settings.state_db)

    g = gates.halt_file_absent(settings.halt_file)
    log.info("halt_file: %s", g.reason)
    if not g.ok:
        return 0

    if signal_file is None:
        g = gates.market_is_open()
        log.info("market_is_open: %s", g.reason)
        if not g.ok:
            return 0
    else:
        log.info("market_is_open: skipped (--signal-file offline mode)")

    if signal_file is not None:
        fetched_signals: list[FetchedSignal] = [_load_signal_from_file(signal_file)]
    else:
        poller = SignalPoller(settings.fib_repo, settings.fib_branch, settings.github_token)
        fetched_signals = list(poller.poll())

    if not fetched_signals:
        log.info("no signals this tick")
        return 0

    broker: IBKRBroker | None = None
    counts: dict[str, int] = {}
    try:
        for fetched in fetched_signals:
            signal = fetched.signal
            gate_log: list[str] = []

            if signal.is_expired():
                store.record(signal.signal_id, "expired", notes=f"expires_at={signal.expires_at}")
                counts["expired"] = counts.get("expired", 0) + 1
                continue

            if store.already_executed(signal.signal_id):
                log.info("skip duplicate %s", signal.signal_id)
                counts["duplicate"] = counts.get("duplicate", 0) + 1
                continue

            if signal.action == Action.HOLD:
                store.record(signal.signal_id, "hold")
                _write_receipt(settings, fetched, "hold", ["action=HOLD"])
                counts["hold"] = counts.get("hold", 0) + 1
                continue

            if signal_file is not None and not settings.execute:
                gate_log.append("offline --signal-file dry-run: skipping broker gates")
                note = (
                    f"offline dry-run {signal.action.value} "
                    f"{signal.qty_shares}@{signal.limit_price}"
                )
                store.record(signal.signal_id, "dry-run", notes=note)
                _write_receipt(settings, fetched, "dry-run", gate_log)
                counts["dry-run"] = counts.get("dry-run", 0) + 1
                continue

            if broker is None:
                broker = IBKRBroker(settings.ib_host, settings.ib_port, settings.ib_client_id)
                await broker.connect()

            status = await _handle_tradeable(settings, store, broker, fetched, gate_log)
            _write_receipt(settings, fetched, status, gate_log)
            counts[status] = counts.get(status, 0) + 1
    finally:
        if broker is not None:
            await broker.disconnect()

    log.info("tick summary: %s", counts)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--signal-file", type=Path, help="read a single signal from a local JSON file"
    )
    args = parser.parse_args()

    _setup_logging()
    settings = Settings.load()
    log.info("starting tick at %s (EXECUTE=%s)", datetime.now(UTC).isoformat(), settings.execute)
    return asyncio.run(_run(settings, args.signal_file))


if __name__ == "__main__":
    sys.exit(main())
