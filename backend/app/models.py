"""Persistence schema.

The design splits cleanly in two, and that split is the reason the system
scales:

  Shared tables  (symbols, daily_bars, symbol_stats, change_events)
      Sized by the number of *instruments*, which is bounded at a few thousand
      for NSE. Computed once and read by every user.

  Personal tables  (users, watchlists, watchlist_items, user_symbol_seen)
      Sized by the number of *users*, which is not bounded. But they hold no
      market data and require no computation, so they stay cheap.

The expensive work happens once per symbol. Personalisation is a join.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.market.calendar import IST


def _now_ist() -> datetime:
    """Wall clock in IST, naive, like every other datetime in the system.

    These columns previously defaulted to datetime.utcnow, which stored UTC
    into a schema whose every other timestamp -- quotes, watermarks, event
    times -- is naive IST. Anything serialising them then had to either label
    UTC as IST (wrong by five and a half hours) or carry two conflicting
    meanings for the same type.

    This is the one place that reads the wall clock rather than the
    provider's now(). Column defaults have no route to the provider, and
    these are bookkeeping timestamps -- when a row was written -- not market
    observations, so they do not belong on the replay clock.
    """
    return datetime.now(IST).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------
# Shared: one row per instrument, read by everyone
# --------------------------------------------------------------------------


class Symbol(Base):
    __tablename__ = "symbols"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    exchange: Mapped[str] = mapped_column(String(16), default="NSE")
    sector: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # The benchmark this symbol's beta is measured against. Stored per symbol
    # rather than hardcoded, so a banking stock can be measured against Bank
    # Nifty while the rest use the broad index.
    benchmark: Mapped[str] = mapped_column(String(32), default="^NSEI")


class DailyBar(Base):
    """End-of-day OHLCV, already adjusted for corporate actions.

    adj_close is what every return calculation reads. close is kept raw so the
    adjustment remains auditable: if a split adjustment is later found to be
    wrong, we can recompute from the original prints rather than having
    silently destroyed them.
    """

    __tablename__ = "daily_bars"

    symbol: Mapped[str] = mapped_column(String(32), ForeignKey("symbols.symbol"), primary_key=True)
    bar_date: Mapped[date] = mapped_column(Date, primary_key=True)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    adj_close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float)


class SymbolStats(Base):
    """Trailing statistics, recomputed nightly after the close.

    Precomputing these is what keeps the live path cheap: evaluating a tick is
    then one subtraction and one division, not a 30-day window scan.
    """

    __tablename__ = "symbol_stats"

    symbol: Mapped[str] = mapped_column(String(32), ForeignKey("symbols.symbol"), primary_key=True)
    mean_ret_30d: Mapped[float] = mapped_column(Float, default=0.0)
    std_ret_30d: Mapped[float] = mapped_column(Float, default=0.0)
    avg_vol_20d: Mapped[float] = mapped_column(Float, default=0.0)
    high_52w: Mapped[float] = mapped_column(Float, default=0.0)
    low_52w: Mapped[float] = mapped_column(Float, default=0.0)
    beta_60d: Mapped[float] = mapped_column(Float, default=1.0)
    # Number of observations the above are based on. A beta computed from 8
    # days is not trustworthy and the detector should know that.
    sample_size: Mapped[int] = mapped_column(Integer, default=0)
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=_now_ist)


class CorporateAction(Base):
    """Splits, bonuses, dividends.

    Ingested ahead of the ex-date wherever possible. The nightly job uses these
    to back-adjust daily_bars.adj_close so that a 1:5 split does not appear in
    the return series as an 80% crash.
    """

    __tablename__ = "corporate_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), ForeignKey("symbols.symbol"), index=True)
    ex_date: Mapped[date] = mapped_column(Date, index=True)
    action_type: Mapped[str] = mapped_column(String(16))  # split | bonus | dividend
    # For a 1:5 split, ratio = 5.0 (one old share becomes five new).
    ratio: Mapped[float] = mapped_column(Float, default=1.0)
    amount: Mapped[float] = mapped_column(Float, default=0.0)  # dividend per share

    __table_args__ = (UniqueConstraint("symbol", "ex_date", "action_type"),)


class ChangeEventRow(Base):
    """A materialised change event, computed once and shared by all watchers.

    This is the table that makes personalisation cheap. Detecting that TCS
    moved 2.1 sigma happens once, no matter how many thousand users watch it.
    """

    __tablename__ = "change_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), ForeignKey("symbols.symbol"))
    event_type: Mapped[str] = mapped_column(String(32))
    severity: Mapped[float] = mapped_column(Float)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    session_date: Mapped[date] = mapped_column(Date, index=True)
    explanation: Mapped[str] = mapped_column(String(512))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    # Enforced unique so a restarted or duplicated worker cannot double-emit.
    # Idempotency is a property of the schema, not of worker discipline.
    dedupe_key: Mapped[str] = mapped_column(String(160), unique=True)

    __table_args__ = (
        Index("ix_events_symbol_time", "symbol", "occurred_at"),
    )


# --------------------------------------------------------------------------
# Personal: one row per user, holds no market data
# --------------------------------------------------------------------------


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now_ist)

    watchlists: Mapped[list["Watchlist"]] = relationship(back_populates="user")


class Watchlist(Base):
    __tablename__ = "watchlists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(64), default="My watchlist")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now_ist)

    user: Mapped["User"] = relationship(back_populates="watchlists")
    items: Mapped[list["WatchlistItem"]] = relationship(
        back_populates="watchlist", cascade="all, delete-orphan"
    )


class WatchlistItem(Base):
    __tablename__ = "watchlist_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    watchlist_id: Mapped[int] = mapped_column(Integer, ForeignKey("watchlists.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(32), ForeignKey("symbols.symbol"))
    added_at: Mapped[datetime] = mapped_column(DateTime, default=_now_ist)
    # Price when the symbol was added. Lets the UI answer "how has this done
    # since I started caring about it", which is a different and often more
    # useful question than "how has this done today".
    price_at_add: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Optional user-declared entry price, enabling the crossed_cost_basis
    # event without requiring a full portfolio feature.
    cost_basis: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str | None] = mapped_column(String(280), nullable=True)

    watchlist: Mapped["Watchlist"] = relationship(back_populates="items")

    __table_args__ = (UniqueConstraint("watchlist_id", "symbol"),)


class UserSymbolSeen(Base):
    """The read watermark. The single most important table in the system.

    Conventional watchlists are stateless: they render absolute market state,
    identical for every user. That is why they cannot answer "what changed
    since I last looked" without falling back to "since yesterday's close",
    which is not the same question.

    This table makes the diff personal. It records, per user per symbol, the
    moment that user last actually saw the row and the price they saw. The
    digest is then computed against *that*, not against a global reference.

    Keyed by user rather than by device, which is the whole cross-device
    story: read on your phone, and your laptop reflects it. last_seen_at is
    advanced monotonically (GREATEST semantics) so a stale tab that has been
    open since morning cannot resurrect items already read elsewhere.
    """

    __tablename__ = "user_symbol_seen"

    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), ForeignKey("symbols.symbol"), primary_key=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime)
    last_seen_price: Mapped[float | None] = mapped_column(Float, nullable=True)
