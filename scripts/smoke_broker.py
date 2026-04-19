"""Smoke test for the IBKR connection.

Connects to IB Gateway on IB_HOST:IB_PORT (default 127.0.0.1:4002 for paper),
prints managed accounts + buying power, disconnects. Does NOT place orders.

Usage:
    uv run python scripts/smoke_broker.py
"""

from __future__ import annotations

import asyncio
import logging
import sys

from executor.broker.ibkr import IBKRBroker
from executor.config import Settings


async def _run() -> int:
    settings = Settings.load()
    print(f"connecting {settings.ib_host}:{settings.ib_port} clientId={settings.ib_client_id}")
    broker = IBKRBroker(settings.ib_host, settings.ib_port, settings.ib_client_id)
    await broker.connect()
    try:
        bp = await broker.get_buying_power()
        print(f"buying power: ${bp:,.2f}")
    finally:
        await broker.disconnect()
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
