"""Signal contract — mirror of the schema in fib-accumulator/docs/executor-plan.md.

The contract is the stable boundary between producer and executor. Bumping the
major version (e.g. "1.0" -> "2.0") is a breaking change and the executor MUST
refuse unknown majors rather than guess.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

SUPPORTED_SCHEMA_MAJOR = 1


class Action(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class SignalContext(BaseModel):
    model_config = ConfigDict(extra="allow")

    phase: str | None = None
    fib_level: int | None = None
    fg_index: int | None = None
    bullets_spent: float | None = None
    crossover_date: str | None = None
    crossover_price: float | None = None


class Signal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str
    signal_id: str
    issued_at: datetime
    ticker: str
    action: Action
    qty_shares: float | None = None
    notional_usd: float | None = None
    limit_price: float | None = None
    reference_price: float
    confidence: str | None = None
    expires_at: datetime
    context: SignalContext = Field(default_factory=SignalContext)

    @field_validator("schema_version")
    @classmethod
    def _check_major(cls, v: str) -> str:
        major = v.split(".", 1)[0]
        if not major.isdigit() or int(major) != SUPPORTED_SCHEMA_MAJOR:
            raise ValueError(
                f"unsupported schema_version {v!r} (expected major {SUPPORTED_SCHEMA_MAJOR})"
            )
        return v

    @field_validator("ticker")
    @classmethod
    def _upper_ticker(cls, v: str) -> str:
        return v.strip().upper()

    def is_expired(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        return now >= self.expires_at

    def is_tradeable(self) -> bool:
        return self.action in (Action.BUY, Action.SELL) and not self.is_expired()
