"""Print IBKR account summary: net liq, cash, buying power, positions.

Usage:
    uv run python scripts/account_summary.py
"""

from __future__ import annotations

import asyncio
import logging
import sys

from ib_async import IB

from executor.config import Settings


async def _run() -> int:
    settings = Settings.load()
    ib = IB()
    await ib.connectAsync(settings.ib_host, settings.ib_port, clientId=settings.ib_client_id)
    try:
        summary = await ib.accountSummaryAsync()
        wanted = {"NetLiquidation", "BuyingPower", "TotalCashValue", "AvailableFunds"}
        for row in summary:
            if row.tag in wanted:
                print(f"{row.tag:20s} {row.currency:4s} {float(row.value):>15,.2f}")

        positions = await ib.reqPositionsAsync()
        print()
        if positions:
            print("Positions:")
            for p in positions:
                print(
                    f"  {p.contract.symbol:6s} qty={p.position:>8.2f} "
                    f"avgCost={p.avgCost:>10.4f}"
                )
        else:
            print("Positions: (none)")
    finally:
        ib.disconnect()
    return 0


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
