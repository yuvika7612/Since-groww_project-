"""Tests for digest assembly against storage.

Two things are being protected here. The first is the personal diff: the
digest must be computed against what *this* user last saw, not against the
session close. The second is the query shape, because the moment assembly
costs one query per symbol, the architecture the rest of the system is built
around has quietly stopped being true.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta

from sqlalchemy import event, select

from app.cache import InMemoryCache
from app.detect.events import EventType
from app.digest.assembler import assemble_digest
from app.digest.seen import SeenEntry, mark_seen
from app.models import ChangeEventRow, SymbolStats, Watchlist
from app.providers.base import Freshness, Quote
from tests.conftest import make_symbols, make_user, make_watchlist

NOW = datetime(2026, 9, 4, 14, 5)  # a Friday, mid-session
TODAY = date(2026, 9, 4)


def quote(
    symbol: str,
    price: float,
    previous_close: float,
    as_of: datetime = NOW,
    freshness: Freshness = Freshness.LIVE,
) -> Quote:
    return Quote(
        symbol=symbol,
        price=price,
        open=previous_close,
        previous_close=previous_close,
        volume=1_000_000.0,
        as_of=as_of,
        source="replay",
        freshness=freshness,
    )


def add_event(
    session,
    symbol: str,
    occurred_at: datetime,
    etype: EventType = EventType.ABNORMAL_MOVE,
    severity: float = 0.8,
) -> None:
    session.add(
        ChangeEventRow(
            symbol=symbol,
            event_type=etype.value,
            severity=severity,
            occurred_at=occurred_at,
            session_date=occurred_at.date(),
            explanation=f"{etype.value} on {symbol}",
            payload={},
            dedupe_key=f"{symbol}:{etype.value}:{occurred_at.isoformat()}",
        )
    )
    session.flush()


@contextmanager
def count_queries(session):
    counter = {"n": 0}
    engine = session.get_bind()

    def before(conn, cursor, statement, parameters, context, executemany):
        counter["n"] += 1

    event.listen(engine, "before_cursor_execute", before)
    try:
        yield counter
    finally:
        event.remove(engine, "before_cursor_execute", before)


def build(session, symbols, cost_basis=None):
    user = make_user(session)
    make_symbols(session, *symbols)
    make_watchlist(session, user, symbols, cost_basis=cost_basis)
    return user


def row_for(digest, symbol):
    everything = digest.needs_attention + digest.quiet + digest.degraded
    return next(r for r in everything if r.symbol == symbol)


# --- The personal diff ------------------------------------------------------


def test_a_never_seen_symbol_has_no_personal_baseline(session):
    """seen_at stays None so the UI can say "new to your list".

    Falling back to the session close is the only honest reference, but the
    row must not imply the user has a reading history it does not have.
    """
    user = build(session, ["TCS"])
    cache = InMemoryCache()
    cache.set_quote("TCS", quote("TCS", 4000.0, 3900.0))

    digest = assemble_digest(session, cache, user.id, None, NOW)

    row = row_for(digest, "TCS")
    assert row.seen_at is None
    # 4000 vs the 3900 session close.
    assert abs(row.change_since_seen - (100 / 3900)) < 1e-9


def test_change_is_measured_from_the_price_the_user_actually_saw(session):
    """The claim the product is built on.

    The session close is 3900 and the price is 4000, but this user last saw
    3950. Their change is +1.27%, not +2.56%. Every other watchlist would
    show them the second number.
    """
    user = build(session, ["TCS"])
    mark_seen(session, user.id, [SeenEntry("TCS", NOW - timedelta(hours=2), 3950.0)], NOW)
    cache = InMemoryCache()
    cache.set_quote("TCS", quote("TCS", 4000.0, 3900.0))

    digest = assemble_digest(session, cache, user.id, None, NOW)

    row = row_for(digest, "TCS")
    assert row.seen_at == NOW - timedelta(hours=2)
    assert abs(row.change_since_seen - (50 / 3950)) < 1e-9


def test_events_the_user_has_already_read_are_not_resurfaced(session):
    user = build(session, ["TCS"])
    add_event(session, "TCS", NOW - timedelta(hours=3))  # before they looked
    add_event(session, "TCS", NOW - timedelta(minutes=30))  # after
    mark_seen(session, user.id, [SeenEntry("TCS", NOW - timedelta(hours=1), 3950.0)], NOW)

    cache = InMemoryCache()
    cache.set_quote("TCS", quote("TCS", 4000.0, 3900.0))
    digest = assemble_digest(session, cache, user.id, None, NOW)

    row = row_for(digest, "TCS")
    assert len(row.events) == 1
    assert row.events[0].occurred_at == NOW - timedelta(minutes=30)


def test_the_same_event_is_unread_for_one_user_and_read_for_another(session):
    """Signal computed once, relevance decided per user."""
    reader = build(session, ["TCS"])
    other = make_user(session, email="other@example.com")
    make_watchlist(session, other, ["TCS"])
    add_event(session, "TCS", NOW - timedelta(minutes=30))
    mark_seen(session, reader.id, [SeenEntry("TCS", NOW, 4000.0)], NOW)

    cache = InMemoryCache()
    cache.set_quote("TCS", quote("TCS", 4000.0, 3900.0))

    read = assemble_digest(session, cache, reader.id, None, NOW)
    unread = assemble_digest(session, cache, other.id, None, NOW)

    assert row_for(read, "TCS").events == []
    assert len(row_for(unread, "TCS").events) == 1


# --- Data quality -----------------------------------------------------------


def test_a_symbol_with_no_quote_is_degraded_not_priced_at_zero(session):
    """An absent price must never be ranked as though it were a flat market."""
    user = build(session, ["TCS"])

    digest = assemble_digest(session, InMemoryCache(), user.id, None, NOW)

    assert [r.symbol for r in digest.degraded] == ["TCS"]
    row = row_for(digest, "TCS")
    assert row.freshness is Freshness.UNAVAILABLE
    assert row.change_since_seen is None
    assert row.data_note


def test_freshness_is_recomputed_on_read_not_trusted_from_the_cache(session):
    """A quote cached as LIVE an hour ago is stale by the time it is read."""
    user = build(session, ["TCS"])
    cache = InMemoryCache()
    cache.set_quote(
        "TCS",
        quote("TCS", 4000.0, 3900.0, as_of=NOW - timedelta(hours=1), freshness=Freshness.LIVE),
    )

    digest = assemble_digest(session, cache, user.id, None, NOW)

    assert row_for(digest, "TCS").freshness is Freshness.STALE
    assert [r.symbol for r in digest.degraded] == ["TCS"]


# --- Personal events --------------------------------------------------------


def test_crossing_cost_basis_is_computed_per_user_not_stored_shared(session):
    user = build(session, ["TCS"], cost_basis={"TCS": 3980.0})
    mark_seen(session, user.id, [SeenEntry("TCS", NOW - timedelta(hours=1), 3950.0)], NOW)
    cache = InMemoryCache()
    cache.set_quote("TCS", quote("TCS", 4000.0, 3900.0))

    digest = assemble_digest(session, cache, user.id, None, NOW)

    types = {e.type for e in row_for(digest, "TCS").events}
    assert EventType.CROSSED_COST_BASIS in types
    # It is personal, so it must not have been written to the shared table.
    assert session.query(ChangeEventRow).count() == 0


def test_cost_basis_severity_is_scored_against_the_symbols_own_volatility(session):
    """The same 2% crossing is decisive for one symbol and noise for another.

    A raw percentage would rank these identically, which is the mistake this
    whole product is arguing against. The calm symbol's crossing is four sigma
    of its normal day; the volatile one's is a third of a sigma.
    """
    user = make_user(session)
    make_symbols(session, "CALM", "WILD")
    make_watchlist(session, user, ["CALM", "WILD"], cost_basis={"CALM": 100.0, "WILD": 100.0})
    session.add(SymbolStats(symbol="CALM", std_ret_30d=0.005, avg_vol_20d=1e6))
    session.add(SymbolStats(symbol="WILD", std_ret_30d=0.060, avg_vol_20d=1e6))
    session.flush()

    mark_seen(
        session,
        user.id,
        [SeenEntry("CALM", NOW - timedelta(hours=1), 98.0),
         SeenEntry("WILD", NOW - timedelta(hours=1), 98.0)],
        NOW,
    )
    cache = InMemoryCache()
    for symbol in ("CALM", "WILD"):
        cache.set_quote(symbol, quote(symbol, 102.0, 98.0))

    digest = assemble_digest(session, cache, user.id, None, NOW)

    def basis_event(symbol):
        return next(
            e for e in row_for(digest, symbol).events
            if e.type is EventType.CROSSED_COST_BASIS
        )

    assert basis_event("CALM").severity > basis_event("WILD").severity
    assert basis_event("CALM").payload["sigma"] > basis_event("WILD").payload["sigma"]


def test_cost_basis_crossing_is_still_reported_without_statistics(session):
    """A newly listed symbol has no sigma, but the crossing still happened.

    It gets the base severity and no magnitude claim, rather than being
    silently dropped or given a fabricated rank.
    """
    user = build(session, ["TCS"], cost_basis={"TCS": 3980.0})
    mark_seen(session, user.id, [SeenEntry("TCS", NOW - timedelta(hours=1), 3950.0)], NOW)
    cache = InMemoryCache()
    cache.set_quote("TCS", quote("TCS", 4000.0, 3900.0))

    digest = assemble_digest(session, cache, user.id, None, NOW)

    crossing = next(
        e for e in row_for(digest, "TCS").events
        if e.type is EventType.CROSSED_COST_BASIS
    )
    assert crossing.severity == 0.5
    assert "sigma" not in crossing.payload


def test_cost_basis_does_not_refire_while_the_position_stays_above(session):
    """Already above when they last looked is not news."""
    user = build(session, ["TCS"], cost_basis={"TCS": 3900.0})
    mark_seen(session, user.id, [SeenEntry("TCS", NOW - timedelta(hours=1), 3950.0)], NOW)
    cache = InMemoryCache()
    cache.set_quote("TCS", quote("TCS", 4000.0, 3800.0))

    digest = assemble_digest(session, cache, user.id, None, NOW)

    types = {e.type for e in row_for(digest, "TCS").events}
    assert EventType.CROSSED_COST_BASIS not in types


# --- Shape ------------------------------------------------------------------


def test_an_empty_watchlist_produces_an_empty_digest(session):
    user = make_user(session)
    make_symbols(session, "TCS")
    make_watchlist(session, user, [])

    digest = assemble_digest(session, InMemoryCache(), user.id, None, NOW)

    assert digest.needs_attention == []
    assert digest.quiet == []
    assert digest.degraded == []


def test_another_users_watchlist_id_yields_nothing(session):
    """Assembly refuses to cross the user boundary on its own."""
    build(session, ["TCS"])
    intruder = make_user(session, email="intruder@example.com")
    owned = session.scalars(select(Watchlist)).first()

    digest = assemble_digest(session, InMemoryCache(), intruder.id, owned.id, NOW)

    assert digest.needs_attention == []
    assert digest.quiet == []


def test_assembly_query_count_does_not_grow_with_the_watchlist(session):
    """The ceiling that stops an N+1 being reintroduced.

    Fifty symbols must cost the same handful of queries as one. If this test
    starts failing, someone has put a per-symbol lookup inside the loop and
    the shared/personal split has stopped paying for itself.
    """
    symbols = [f"SYM{i:03d}" for i in range(50)]
    user = build(session, symbols)
    cache = InMemoryCache()
    for i, symbol in enumerate(symbols):
        cache.set_quote(symbol, quote(symbol, 100.0 + i, 100.0))
        add_event(session, symbol, NOW - timedelta(minutes=10))
    mark_seen(
        session,
        user.id,
        [SeenEntry(s, NOW - timedelta(hours=1), 99.0) for s in symbols],
        NOW,
    )

    with count_queries(session) as counter:
        digest = assemble_digest(session, cache, user.id, None, NOW)

    assert len(digest.needs_attention) + len(digest.quiet) == 50
    assert counter["n"] < 8, f"assembly issued {counter['n']} queries for 50 symbols"
