"""Configuration loading.

Reads `.env.local` (if present) then `.env` via `python-dotenv`, and exposes a
frozen `Settings` dataclass. Defaults are chosen so an unconfigured run is safe
(dry-run, paper port).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _load_env(repo_root: Path) -> None:
    load_dotenv(repo_root / ".env.local", override=False)
    load_dotenv(repo_root / ".env", override=False)


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    repo_root: Path

    # Signal source
    fib_repo: str
    fib_branch: str
    github_token: str | None

    # IBKR
    ib_host: str
    ib_port: int
    ib_client_id: int

    # Execution
    execute: bool
    max_reference_drift_pct: float
    buying_power_buffer: float
    order_poll_timeout_s: int

    # Paths
    state_db: Path
    receipts_dir: Path
    halt_file: Path

    @classmethod
    def load(cls, repo_root: Path | None = None) -> Settings:
        root = repo_root or Path(__file__).resolve().parents[2]
        _load_env(root)

        state_db = Path(os.environ.get("STATE_DB", "state/executed_signals.db"))
        receipts_dir = Path(os.environ.get("RECEIPTS_DIR", "receipts"))
        halt_file = Path(os.environ.get("HALT_FILE", "HALT"))

        return cls(
            repo_root=root,
            fib_repo=os.environ.get("FIB_REPO", ""),
            fib_branch=os.environ.get("FIB_BRANCH", "main"),
            github_token=os.environ.get("GITHUB_TOKEN") or None,
            ib_host=os.environ.get("IB_HOST", "127.0.0.1"),
            ib_port=_int("IB_PORT", 4002),
            ib_client_id=_int("IB_CLIENT_ID", 17),
            execute=_bool("EXECUTE", False),
            max_reference_drift_pct=_float("MAX_REFERENCE_DRIFT_PCT", 5.0),
            buying_power_buffer=_float("BUYING_POWER_BUFFER", 1.02),
            order_poll_timeout_s=_int("ORDER_POLL_TIMEOUT_S", 300),
            state_db=(root / state_db) if not state_db.is_absolute() else state_db,
            receipts_dir=(root / receipts_dir) if not receipts_dir.is_absolute() else receipts_dir,
            halt_file=(root / halt_file) if not halt_file.is_absolute() else halt_file,
        )
