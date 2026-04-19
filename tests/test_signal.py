from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from executor.signal import Action, Signal

FIXTURE = Path(__file__).parent / "fixtures" / "sample_signal.json"


def _load() -> dict:
    return json.loads(FIXTURE.read_text())


def test_fixture_parses():
    signal = Signal.model_validate(_load())
    assert signal.ticker == "TQQQ"
    assert signal.action == Action.BUY
    assert signal.limit_price == 48.30


def test_rejects_unknown_schema_major():
    raw = _load()
    raw["schema_version"] = "2.0"
    with pytest.raises(ValidationError):
        Signal.model_validate(raw)


def test_rejects_unknown_top_level_field():
    raw = _load()
    raw["secret_payload"] = "x"
    with pytest.raises(ValidationError):
        Signal.model_validate(raw)


def test_hold_is_not_tradeable():
    raw = _load()
    raw["action"] = "HOLD"
    signal = Signal.model_validate(raw)
    assert not signal.is_tradeable()


def test_expired_signal_is_not_tradeable():
    raw = _load()
    past = datetime.now(UTC) - timedelta(days=1)
    raw["expires_at"] = past.isoformat()
    signal = Signal.model_validate(raw)
    assert signal.is_expired()
    assert not signal.is_tradeable()


def test_ticker_is_uppercased():
    raw = _load()
    raw["ticker"] = "tqqq"
    signal = Signal.model_validate(raw)
    assert signal.ticker == "TQQQ"
