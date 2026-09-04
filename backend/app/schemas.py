"""Response and request shapes.

No ORM object ever reaches a response. Every model here is explicit, so a
column added to models.py does not silently widen the public API, and the
frontend can generate types from the OpenAPI schema without inheriting the
database layout.

Every datetime crosses the wire as ISO 8601 *with an offset*. Internally the
system speaks naive IST (see market/calendar.py); `ist()` below is the single
place that boundary is crossed. A bare naive timestamp in an API response is
an invitation for the client to guess, and clients guess UTC.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.market.calendar import IST


def ist(when: datetime | None) -> datetime | None:
    """Attach the IST offset to a naive internal timestamp."""
    if when is None:
        return None
    if when.tzinfo is None:
        return when.replace(tzinfo=IST)
    return when.astimezone(IST)


# --- Auth -------------------------------------------------------------------


class DevLoginRequest(BaseModel):
    email: str


class TokenResponse(BaseModel):
    user_id: int
    token: str


# --- Symbols ----------------------------------------------------------------


class SymbolOut(BaseModel):
    symbol: str
    name: str
    exchange: str


class SymbolStatsOut(BaseModel):
    mean_ret_30d: float
    std_ret_30d: float
    avg_vol_20d: float
    high_52w: float
    low_52w: float
    beta_60d: float
    sample_size: int
    computed_at: datetime | None = None
    # The band a normal day for this symbol falls in, which is the whole
    # point of storing per-symbol statistics: it is what makes "4% is a lot"
    # answerable rather than a guess.
    expected_daily_move: float


class EventOut(BaseModel):
    type: str
    severity: float
    explanation: str
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime | None = None


class SymbolDetail(BaseModel):
    symbol: str
    name: str
    exchange: str
    sector: str | None = None
    benchmark: str
    stats: SymbolStatsOut | None = None
    recent_events: list[EventOut] = Field(default_factory=list)


# --- Watchlists -------------------------------------------------------------


class WatchlistSummary(BaseModel):
    id: int
    name: str
    item_count: int


class CreateWatchlistRequest(BaseModel):
    name: str = "My watchlist"


class AddItemRequest(BaseModel):
    symbol: str
    cost_basis: float | None = None
    note: str | None = None


class UpdateItemRequest(BaseModel):
    cost_basis: float | None = None
    note: str | None = None


class WatchlistItemOut(BaseModel):
    symbol: str
    name: str
    cost_basis: float | None = None
    note: str | None = None
    price_at_add: float | None = None
    added_at: datetime | None = None
    # Null rather than absent when we have no quote. A missing price is a
    # state the UI must render as "no number", never as zero.
    price: float | None = None
    as_of: datetime | None = None
    source: str | None = None
    freshness: str | None = None


class WatchlistDetail(BaseModel):
    id: int
    name: str
    items: list[WatchlistItemOut] = Field(default_factory=list)


# --- Digest -----------------------------------------------------------------


class MarketOut(BaseModel):
    index_symbol: str
    index_return: float
    breadth: float
    is_market_wide: bool
    headline: str


class DigestRowOut(BaseModel):
    symbol: str
    name: str
    # None when freshness is "unavailable". The assembler carries a 0.0
    # placeholder because DigestRow.price is non-optional; it must never
    # reach a client as a real price.
    price: float | None = None
    as_of: datetime | None = None
    source: str | None = None
    freshness: str
    change_since_seen: float | None = None
    seen_at: datetime | None = None
    score: float
    primary_reason: str | None = None
    events: list[EventOut] = Field(default_factory=list)
    data_note: str | None = None


class DigestOut(BaseModel):
    generated_at: datetime
    market_state: str
    market: MarketOut | None = None
    needs_attention: list[DigestRowOut] = Field(default_factory=list)
    quiet: list[DigestRowOut] = Field(default_factory=list)
    quiet_summary: str = ""
    degraded: list[DigestRowOut] = Field(default_factory=list)


# --- Seen -------------------------------------------------------------------


class SeenEntryIn(BaseModel):
    symbol: str
    seen_at: datetime
    price: float | None = None


class SeenRequest(BaseModel):
    entries: list[SeenEntryIn] = Field(default_factory=list)


class SeenResponse(BaseModel):
    updated: int
    # Symbols the user does not watch. Reported rather than silently dropped,
    # because a client whose writes vanish has no way to discover it is out
    # of sync.
    rejected: list[str] = Field(default_factory=list)


# --- Health and demo --------------------------------------------------------


class HealthOut(BaseModel):
    status: str
    market_state: str
    provider: str
    hot_set_size: int
    last_poll_at: datetime | None = None
    last_poll_symbol_count: int
    last_poll_rejected_count: int


class InjectFaultRequest(BaseModel):
    kind: str
    symbol: str | None = None
    magnitude: float = 1.0
    duration_minutes: int | None = None


class SeekRequest(BaseModel):
    to: datetime


class ScenarioFault(BaseModel):
    kind: str
    symbol: str | None = None
    magnitude: float = 1.0
    duration_minutes: int | None = None


class ScenarioOut(BaseModel):
    key: str
    title: str
    description: str
    seek_to: datetime
    faults: list[ScenarioFault] = Field(default_factory=list)
