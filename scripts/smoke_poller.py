"""Smoke test for the GitHub signal poller.

Exercises: env loading, PAT auth, fib-accumulator `signals/` listing, JSON fetch
+ schema validation. Does NOT hit IBKR. Prints what it finds; no side effects.

Usage:
    uv run python scripts/smoke_poller.py
"""

from __future__ import annotations

import logging
import sys

from executor.config import Settings
from executor.poller import SignalPoller


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.load()
    print(f"repo={settings.fib_repo} branch={settings.fib_branch}")
    print(f"token set: {bool(settings.github_token)}")
    poller = SignalPoller(settings.fib_repo, settings.fib_branch, settings.github_token)
    count = 0
    for fetched in poller.poll():
        count += 1
        s = fetched.signal
        print(
            f"  {s.signal_id:30s} {s.action.value:5s} {s.ticker:6s} "
            f"qty={s.qty_shares} limit={s.limit_price} expires={s.expires_at.isoformat()}"
        )
    print(f"total signals fetched: {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
