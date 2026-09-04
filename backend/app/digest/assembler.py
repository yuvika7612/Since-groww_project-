"""Digest assembly against the database and cache.

`digest/service.py` is pure: it ranks and collapses rows and knows nothing
about storage. This module is the glue that loads the inputs it needs.

The whole file is deliberately shaped around one constraint: the number of
queries must not grow with the number of symbols. Everything is batched, and
`tests/test_assembler.py` asserts a ceiling so nobody reintroduces an N+1
later. That ceiling is the architecture, written down as a test.

Nothing here loops over users. This runs for one user, joining against events
that were computed once for everybody.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cache import Cache
from app.config import settings
from app.detect.events import ChangeEvent, EventType
from app.detect.signals import MIN_SIGMA, saturate
from app.digest.service import (
    Digest,
    DigestRow,
    MarketContext,
    build_digest,
    compute_market_context,
)
from app.market.calendar import is_trading_day, previous_trading_day
from app.models import (
    ChangeEventRow,
    Symbol,
    SymbolStats,
    UserSymbolSeen,
    Watchlist,
    WatchlistItem,
)
from app.providers.base import Freshness, Quote

DEFAULT_BENCHMARK = "^NSEI"


def _session_day(now: datetime):
    """The trading day `now` belongs to.

    On a weekend or a holiday the current session is the previous trading day,
    which is what the user is still looking at.
    """
    today = now.date()
    return today if is_trading_day(today) else previous_trading_day(today)


def _fractional_change(current: float, reference: float | None) -> float | None:
    """Guarded division. A zero or missing reference yields no number at all.

    Returning 0.0 here would be a lie: it says "unchanged" when the truth is
    "we cannot say".
    """
    if reference is None or reference <= 0:
        return None
    return (current - reference) / reference


def _to_event(row: ChangeEventRow) -> ChangeEvent:
    return ChangeEvent(
        symbol=row.symbol,
        type=EventType(row.event_type),
        severity=row.severity,
        occurred_at=row.occurred_at,
        session_date=row.session_date,
        explanation=row.explanation,
        payload=row.payload or {},
    )


def _cost_basis_event(
    symbol: str,
    price: float,
    reference: float | None,
    cost_basis: float,
    stats: SymbolStats | None,
    now: datetime,
    session_date,
) -> ChangeEvent | None:
    """The one event that cannot be shared: it depends on this user's basis.

    Deliberately measured as a *crossing* since the reference price rather
    than as "price is above cost basis". The latter would re-fire every single
    poll for as long as the position stays profitable, which is noise. Having
    crossed the line since you last looked is news exactly once.

    Kept out of change_events because that table is shared by every user, and
    putting a per-user fact in it would collapse the boundary the whole
    architecture rests on.

    Magnitude is scored in units of the symbol's own daily volatility, like
    every other signal in the system. A raw percentage would be wrong in both
    directions: 2% past cost basis is decisive for a stock that moves 0.5% on
    a normal day and inside the noise for one that routinely swings 6%.
    """
    if reference is None or cost_basis <= 0:
        return None

    crossed_up = reference < cost_basis <= price
    crossed_down = reference > cost_basis >= price
    if not (crossed_up or crossed_down):
        return None

    distance = abs(price - cost_basis) / cost_basis
    direction = "above" if crossed_up else "below"
    payload = {"cost_basis": cost_basis, "price": round(price, 4)}
    detail = ""

    if stats is not None and stats.std_ret_30d > MIN_SIGMA:
        # cap=3.0 sigma to match the abnormal-move scoring in detector.py:
        # past three sigma, one crossing is not meaningfully more urgent
        # than another.
        sigma_distance = distance / stats.std_ret_30d
        severity = 0.5 + 0.5 * saturate(sigma_distance, cap=3.0)
        payload["sigma"] = round(sigma_distance, 3)
        detail = f", {sigma_distance:.1f} sigma past it"
    else:
        # Suspended, newly listed, or stats not computed yet. The crossing is
        # still a fact worth reporting; its magnitude is not something we can
        # honestly rank, so it gets the base severity and no bonus.
        severity = 0.5

    return ChangeEvent(
        symbol=symbol,
        type=EventType.CROSSED_COST_BASIS,
        severity=round(severity, 4),
        occurred_at=now,
        session_date=session_date,
        explanation=(
            f"Crossed {direction} your cost basis of {cost_basis:.2f}, "
            f"now {price:.2f}{detail}"
        ),
        payload=payload,
    )


def assemble_digest(
    session: Session,
    cache: Cache,
    user_id: int,
    watchlist_id: int | None,
    now: datetime,
) -> Digest:
    # --- 1. the user's list -------------------------------------------------
    watchlist_query = select(Watchlist.id).where(Watchlist.user_id == user_id)
    if watchlist_id is not None:
        watchlist_query = watchlist_query.where(Watchlist.id == watchlist_id)
    resolved_id = session.scalars(watchlist_query.order_by(Watchlist.id).limit(1)).first()

    if resolved_id is None:
        # No watchlist, or one belonging to somebody else. An empty digest is
        # the honest answer; the caller decides whether that is a 404.
        return build_digest([], market=None, now=now)

    items = session.execute(
        select(
            WatchlistItem.symbol,
            WatchlistItem.cost_basis,
            Symbol.name,
            Symbol.benchmark,
        )
        .join(Symbol, Symbol.symbol == WatchlistItem.symbol)
        .where(WatchlistItem.watchlist_id == resolved_id)
        .order_by(WatchlistItem.symbol)
    ).all()

    if not items:
        return build_digest([], market=None, now=now)

    symbols = [item.symbol for item in items]

    # The index the *majority* of this list is measured against. Stored per
    # symbol so a bank can sit against Bank Nifty, but a digest has one
    # market headline, so one benchmark has to win.
    benchmark = Counter(
        item.benchmark for item in items if item.benchmark
    ).most_common(1)
    index_symbol = benchmark[0][0] if benchmark else DEFAULT_BENCHMARK

    # --- 2. quotes, one call, benchmark included ---------------------------
    quotes = cache.get_quotes(symbols + [index_symbol])

    # --- 3. watermarks, one query ------------------------------------------
    watermarks = {
        row.symbol: row
        for row in session.scalars(
            select(UserSymbolSeen).where(
                UserSymbolSeen.user_id == user_id,
                UserSymbolSeen.symbol.in_(symbols),
            )
        )
    }

    # --- 4. trailing statistics, one query ---------------------------------
    # Scores the cost-basis crossing below in units of each symbol's own
    # volatility, and is what Phase 4's symbol detail endpoint reads to show
    # an expected move range.
    stats_by_symbol: dict[str, SymbolStats] = {
        row.symbol: row
        for row in session.scalars(
            select(SymbolStats).where(SymbolStats.symbol.in_(symbols))
        )
    }

    # --- 5. events, one query ----------------------------------------------
    session_date = _session_day(now)
    session_start = datetime.combine(session_date, time.min)
    # Bounded by the oldest thing any row could need: the earliest watermark,
    # or the start of the session for a symbol that has never been seen.
    # Without a floor this reads the entire event history on every request.
    floor = min(
        [w.last_seen_at for w in watermarks.values()] + [session_start]
    )

    events_by_symbol: dict[str, list[ChangeEvent]] = defaultdict(list)
    event_rows = session.scalars(
        select(ChangeEventRow)
        .where(
            ChangeEventRow.symbol.in_(symbols),
            ChangeEventRow.occurred_at > floor,
        )
        .order_by(ChangeEventRow.occurred_at)
    )
    for row in event_rows:
        watermark = watermarks.get(row.symbol)
        if watermark is not None:
            # Since *this user* last looked. The same event is new to one
            # user and already read by another; that is the product.
            if row.occurred_at <= watermark.last_seen_at:
                continue
        elif row.session_date != session_date:
            # Never seen this symbol, so there is no personal baseline. Show
            # the current session rather than its entire history.
            continue
        events_by_symbol[row.symbol].append(_to_event(row))

    # --- 6. the market's own move ------------------------------------------
    index_quote = quotes.get(index_symbol)
    index_return = (
        _fractional_change(index_quote.price, index_quote.previous_close)
        if index_quote
        else None
    )

    # --- 7. rows ------------------------------------------------------------
    rows: list[DigestRow] = []
    for item in items:
        watermark = watermarks.get(item.symbol)
        seen_at = watermark.last_seen_at if watermark else None
        quote = quotes.get(item.symbol)

        if quote is None:
            # Never polled, or dropped out of the cache entirely. There is no
            # price to show and none will be invented: price is a placeholder
            # the UI must not render while freshness is UNAVAILABLE.
            rows.append(
                DigestRow(
                    symbol=item.symbol,
                    name=item.name,
                    price=0.0,
                    freshness=Freshness.UNAVAILABLE,
                    as_of=now,
                    change_since_seen=None,
                    seen_at=seen_at,
                    events=[],
                    data_note="No price available for this symbol right now",
                )
            )
            continue

        # Freshness is a property of the moment you look, not of the fetch.
        quote = quote.aged(now, settings.stale_after_seconds)

        # Diff against the price this user last saw. Only when they have never
        # seen it does the session close become the reference, and seen_at
        # stays None so the UI can say "new to your list" instead of implying
        # a personal baseline that does not exist.
        reference = (
            watermark.last_seen_price
            if watermark and watermark.last_seen_price
            else quote.previous_close
        )
        change_since_seen = _fractional_change(quote.price, reference)

        events = list(events_by_symbol.get(item.symbol, []))
        if item.cost_basis:
            personal = _cost_basis_event(
                item.symbol,
                quote.price,
                reference,
                item.cost_basis,
                stats_by_symbol.get(item.symbol),
                now,
                session_date,
            )
            if personal is not None:
                events.append(personal)

        rows.append(
            DigestRow(
                symbol=item.symbol,
                name=item.name,
                price=quote.price,
                freshness=quote.freshness,
                as_of=quote.as_of,
                change_since_seen=change_since_seen,
                seen_at=seen_at,
                events=events,
            )
        )

    # --- 8. rank -----------------------------------------------------------
    market: MarketContext | None = None
    if index_return is not None:
        market = compute_market_context(index_symbol, index_return, rows)

    return build_digest(rows, market, now)
