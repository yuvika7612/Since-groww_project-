"""Tests for the poll loop.

Three claims, each one a thing that would be invisible until it mattered:

  a single broken symbol costs one row, never the cycle
  a restarted worker cannot double-emit
  the shipped fixture actually produces the correlation collapse it is for
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.cache import InMemoryCache
from app.detect.detector import detect as real_detect
from app.detect.signals import SymbolStatistics, residual_return
from app.models import Base, ChangeEventRow, Symbol, SymbolStats
from app.providers.base import Freshness, Quote
from workers import ingest

NOW = datetime(2026, 9, 4, 11, 0)  # a Friday, mid-session
TODAY = date(2026, 9, 4)


class StubProvider:
    """Just enough provider to drive one cycle. No secondary_quotes attribute,
    so the reconciliation branch is exercised in its absent-source form."""

    name = "stub"

    def __init__(self, quotes: dict[str, Quote], now: datetime = NOW):
        self._quotes = quotes
        self._now = now

    def now(self) -> datetime:
        return self._now

    def fetch(self, symbols: list[str]) -> dict[str, Quote]:
        return {s: q for s, q in self._quotes.items() if s in symbols}


def quote(symbol: str, price: float, previous_close: float) -> Quote:
    return Quote(
        symbol=symbol,
        price=price,
        open=previous_close,
        previous_close=previous_close,
        volume=1_000_000.0,
        as_of=NOW,
        source="stub",
        freshness=Freshness.LIVE,
    )


@pytest.fixture
def wired(monkeypatch):
    """The worker pointed at a throwaway database and a fresh cache."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    with factory() as setup:
        for symbol in ("TCS", "INFY", "^NSEI"):
            setup.add(Symbol(symbol=symbol, name=symbol, exchange="NSE"))
            setup.add(
                SymbolStats(
                    symbol=symbol,
                    mean_ret_30d=0.0,
                    std_ret_30d=0.010,
                    avg_vol_20d=1_000_000.0,
                    high_52w=9_999.0,
                    low_52w=1.0,
                    beta_60d=1.0,
                    sample_size=60,
                )
            )
        setup.commit()

    cache = InMemoryCache()
    monkeypatch.setattr(ingest, "SessionLocal", factory)
    monkeypatch.setattr(ingest, "cache", cache)
    # Pre-set so _cycle skips its trailing sleep and returns immediately.
    monkeypatch.setattr(ingest, "_shutdown", __import__("threading").Event())
    ingest._shutdown.set()

    return factory, cache


def test_one_broken_symbol_does_not_stop_the_cycle(wired, monkeypatch):
    """A delisted ticker or a mangled frame costs one row, not the session.

    Without the per-symbol guard, whichever symbol happened to sort first
    would decide whether anybody got a digest that day.
    """
    factory, cache = wired
    for symbol in ("TCS", "INFY"):
        cache.add_to_hot_set(symbol)

    def exploding_detect(context, stats):
        if context.symbol == "INFY":
            raise ValueError("simulated bad frame")
        return real_detect(context, stats)

    monkeypatch.setattr(ingest, "detect", exploding_detect)
    monkeypatch.setattr(
        ingest,
        "provider",
        StubProvider(
            {
                "TCS": quote("TCS", 4000.0, 3900.0),   # +2.6%, ~2.6 sigma
                "INFY": quote("INFY", 1800.0, 1720.0),
                "^NSEI": quote("^NSEI", 24500.0, 24500.0),
            }
        ),
    )

    ingest._cycle()  # must not raise

    with factory() as session:
        emitted = {row.symbol for row in session.query(ChangeEventRow)}
    assert "TCS" in emitted, "the healthy symbol was not processed"
    assert "INFY" not in emitted
    # The broken symbol still had its quote cached before detection ran, so
    # the failure is isolated to detection rather than losing the price too.
    assert cache.get_quote("INFY") is not None


def test_restart_does_not_double_emit(wired, monkeypatch):
    """Idempotency is a property of the schema, not of worker discipline.

    Simulated by handing the second pass a fresh, empty set of session keys,
    which is exactly what a restarted process holds before it reloads them.
    The unique constraint on dedupe_key is the only thing standing between
    that and a duplicate alert.
    """
    factory, cache = wired
    cache.add_to_hot_set("TCS")
    monkeypatch.setattr(
        ingest,
        "provider",
        StubProvider({"TCS": quote("TCS", 4000.0, 3900.0)}),
    )

    stats = {"TCS": SymbolStatistics(0.0, 0.010, 1_000_000.0, 9_999.0, 1.0, 1.0)}
    quotes = {"TCS": quote("TCS", 4000.0, 3900.0)}
    counts = ingest.CycleCounts()

    with factory() as session:
        ingest._process_symbol(
            session, "TCS", quotes, {}, stats, {}, set(), TODAY, counts
        )
        session.commit()
        first_pass = [row.dedupe_key for row in session.query(ChangeEventRow)]

        # Same tick again, with the empty key set a restarted process holds.
        ingest._process_symbol(
            session, "TCS", quotes, {}, stats, {}, set(), TODAY, counts
        )
        session.commit()
        second_pass = [row.dedupe_key for row in session.query(ChangeEventRow)]

    # One tick legitimately produces several *different* events; what must not
    # happen is any of them landing twice.
    assert first_pass, "the first pass emitted nothing, so the test proves nothing"
    assert second_pass == first_pass, "restart re-emitted events"
    assert len(set(second_pass)) == len(second_pass), "duplicate dedupe_key persisted"
    assert counts.emitted == len(first_pass)
    assert counts.duplicates == len(first_pass), "the IntegrityError path was never taken"


FIXTURE = Path(__file__).resolve().parent.parent / "data" / "session.jsonl"


@pytest.mark.skipif(
    not FIXTURE.exists(),
    reason="run scripts/seed.py and scripts/record_fixture.py first",
)
def test_the_shipped_fixture_still_produces_the_collapse():
    """The demo's best scenario, pinned so a refactor cannot quietly break it.

    At 11:45 the index is down ~2.2% and every large cap is down by its own
    beta, so their residuals are ~0 and they collapse into one headline. IRFC
    stands still, which its beta says it should not have, so it is the only
    row carrying information.

    If this fails, the fixture no longer demonstrates the thing the product
    is built to do, and the failure would otherwise only be visible live.
    """
    from app.providers.replay import ReplayProvider

    replay = ReplayProvider(FIXTURE, speed=1.0, start_at=datetime(2026, 9, 4, 11, 45))
    quotes = replay.fetch(["^NSEI", "IRFC", "HDFCBANK", "SBIN"])
    assert quotes, "fixture produced no frames at 11:45"

    def day_return(symbol: str) -> float:
        q = quotes[symbol]
        return (q.price - q.previous_close) / q.previous_close

    index_return = day_return("^NSEI")
    assert index_return < -0.015, f"selloff segment missing: index {index_return:.4f}"

    # Betas as recorded when the fixture was written; the point is the shape.
    holdout = residual_return(
        day_return("IRFC"), index_return, SymbolStatistics(0, 0.02, 1, 0, 0, 1.38)
    )
    follower = residual_return(
        day_return("HDFCBANK"), index_return, SymbolStatistics(0, 0.01, 1, 0, 0, 1.13)
    )

    assert abs(holdout) >= 0.015, f"IRFC would not surface: residual {holdout:.4f}"
    assert abs(follower) < 0.015, f"HDFCBANK would not collapse: residual {follower:.4f}"
