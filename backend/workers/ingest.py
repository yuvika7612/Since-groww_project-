"""The poll loop. This is the file that makes the system live.

It is the shared half of the architecture: everything here runs once per
*symbol*, never once per user. A thousand users watching TCS cost exactly one
fetch, one validation and one detection pass.

Three properties are load-bearing:

  One broken symbol cannot stop a cycle. Every per-symbol step is wrapped,
  logged and skipped. A delisted ticker or a mangled frame costs one row.

  Restarting cannot double-emit. Idempotency is a property of the schema --
  the unique constraint on dedupe_key -- not of worker discipline. A caught
  IntegrityError here is the system working, not an error.

  Corporate actions are applied before any return is computed. See
  _apply_corporate_action: on an ex-date the quoted price moves for reasons
  that are not market information, and every stored price level moves with it.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from dataclasses import dataclass, replace
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app import state
from app.cache import cache
from app.config import settings
from app.db import SessionLocal
from app.detect.detector import TickContext, dedupe, detect
from app.detect.events import ChangeEvent, EventType
from app.detect.signals import SymbolStatistics
from app.market import calendar
from app.market.calendar import MarketState
from app.market.validate import reconcile, validate_tick
from app.models import ChangeEventRow, CorporateAction, SymbolStats, WatchlistItem
from app.providers.factory import provider

log = logging.getLogger(__name__)

# Every real upstream charges by the request, so symbols are fetched in
# batches rather than one at a time.
BATCH_SIZE = 50

# Nothing in the hot set means nobody is watching anything. Check back soon
# rather than backing off to the closed-market interval, because a user adding
# their first symbol should not wait ten minutes for a price.
IDLE_SLEEP_SECONDS = 10
ERROR_BACKOFF_SECONDS = 5

_shutdown = threading.Event()
# Serialises the two startup callers of _rebuild_hot_set.
_rebuild_lock = threading.Lock()


@dataclass
class CycleCounts:
    accepted: int = 0
    rejected: int = 0
    emitted: int = 0
    duplicates: int = 0


def _install_signal_handlers() -> None:
    try:
        signal.signal(signal.SIGTERM, lambda *_: _shutdown.set())
        signal.signal(signal.SIGINT, lambda *_: _shutdown.set())
    except ValueError:
        # signal.signal() only works on the main thread. In the demo this
        # worker runs as a daemon thread inside the API process, where uvicorn
        # owns the handlers and the thread dies with the process anyway.
        log.debug("not the main thread; leaving signal handling to the host")


def _chunks(items: list[str], size: int):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _session_date(now: datetime) -> date:
    today = now.date()
    return today if calendar.is_trading_day(today) else calendar.previous_trading_day(today)


def _load_stats(session, symbols: list[str]) -> dict[str, SymbolStatistics]:
    """Every symbol's trailing statistics in one query, not one per tick.

    A symbol with no row gets the degenerate all-zero statistics, whose
    is_scorable is False, so the detector says nothing about it rather than
    guessing from numbers it does not have.
    """
    rows = session.scalars(
        select(SymbolStats).where(SymbolStats.symbol.in_(symbols))
    ).all()
    loaded = {
        row.symbol: SymbolStatistics(
            mean_ret_30d=row.mean_ret_30d,
            std_ret_30d=row.std_ret_30d,
            avg_vol_20d=row.avg_vol_20d,
            high_52w=row.high_52w,
            low_52w=row.low_52w,
            beta_60d=row.beta_60d,
        )
        for row in rows
    }
    degenerate = SymbolStatistics(0.0, 0.0, 0.0, 0.0, 0.0, 1.0)
    return {symbol: loaded.get(symbol, degenerate) for symbol in symbols}


def _load_actions(session, symbols: list[str], session_date: date) -> dict[str, CorporateAction]:
    """Corporate actions effective today, in one query."""
    rows = session.scalars(
        select(CorporateAction).where(
            CorporateAction.symbol.in_(symbols),
            CorporateAction.ex_date == session_date,
        )
    ).all()
    return {row.symbol: row for row in rows}


def _rebuild_hot_set(session) -> list[str]:
    """Repopulate the hot set from what users actually watch.

    The hot set lives in the cache, which is memory. The watchlists live in
    the database, which is durable. After a restart -- or after seeding, which
    writes watchlist rows without ever going through the API -- the two
    disagree and the poller has nothing to fetch.

    The database is the source of truth, so it wins. Refcounts are rebuilt
    from the real number of watchlist rows referencing each symbol, which is
    what the API's increments and decrements would have produced.

    Guarded, because two callers race on startup: the API lifespan calls this
    and so does the worker's first cycle when it finds nothing to poll. Run
    twice, every refcount doubles, and a user removing their only reference to
    a symbol leaves it hot forever -- the exact leak refcounting exists to
    prevent, introduced by the recovery for it.
    """
    with _rebuild_lock:
        already_hot = set(cache.hot_set())
        rows = session.execute(
            select(WatchlistItem.symbol, func.count(WatchlistItem.id))
            .group_by(WatchlistItem.symbol)
        ).all()
        restored = 0
        for symbol, references in rows:
            if symbol in already_hot:
                continue
            for _ in range(references):
                cache.add_to_hot_set(symbol)
            restored += 1
        if restored:
            log.info("rebuilt hot set from watchlists: %d symbols", restored)
        return cache.hot_set()


def _load_session_keys(session, session_date: date) -> set[str]:
    """Dedupe keys already persisted for this session.

    Loaded at the start of every cycle so a restarted worker does not re-emit
    everything that already happened today. The database is the memory; the
    worker holds none of its own across restarts.
    """
    return set(
        session.scalars(
            select(ChangeEventRow.dedupe_key).where(
                ChangeEventRow.session_date == session_date
            )
        )
    )


def _apply_corporate_action(
    action: CorporateAction,
    stats: SymbolStatistics,
    previous_close: float,
) -> tuple[SymbolStatistics, float]:
    """Restate the one price level the nightly job cannot reach.

    A 1:5 split drops the quoted price 80% overnight and nothing happened to
    the value of anyone's holding. Without an adjustment the detector sees the
    largest move it has ever recorded and reports it as the most urgent thing
    in the user's watchlist.

    Only previous_close is adjusted here, and the asymmetry is deliberate.
    previous_close arrives from the live feed, which quotes it in *old* shares
    on an ex-date. The stored statistics do not: adjust_for_corporate_actions
    inside the nightly job already restates the whole series for every known
    action, including one dated after the last bar, so high_52w and low_52w
    are already in new shares by the time this runs.

    Calling restate_statistics here as well divides them a second time.
    Measured on the seeded TCS 1:5: high_52w 855.57 becomes 171.11, the
    post-split price of 759 clears it, and the system reports a new 52-week
    high on the day of a split -- the exact false alarm this code exists to
    prevent, reintroduced by over-correcting for it.

    Operational consequence worth stating: a corporate action ingested after
    the last nightly run leaves the stored levels in old shares, and nothing
    here will catch it. Re-run workers.nightly when actions are ingested.
    """
    if action.action_type not in ("split", "bonus") or action.ratio <= 0:
        return stats, previous_close
    return stats, previous_close / action.ratio


def _persist(
    session,
    symbol: str,
    events: list[ChangeEvent],
    counts: CycleCounts,
) -> list[tuple[str, int, ChangeEvent]]:
    """Write events, tolerating the duplicates the unique constraint catches.

    Each insert gets its own savepoint so one rejected duplicate does not
    poison the transaction for the rest of the batch.
    """
    persisted: list[tuple[str, int, ChangeEvent]] = []
    for event in events:
        row = ChangeEventRow(
            symbol=event.symbol,
            event_type=event.type.value,
            severity=event.severity,
            occurred_at=event.occurred_at,
            session_date=event.session_date,
            explanation=event.explanation,
            payload=event.payload,
            dedupe_key=event.dedupe_key,
        )
        try:
            with session.begin_nested():
                session.add(row)
                session.flush()
        except IntegrityError:
            # Another worker, or this one before a restart, already emitted
            # this exact event. That is the constraint doing its job.
            counts.duplicates += 1
            continue
        persisted.append((symbol, row.id, event))
        counts.emitted += 1
    return persisted


def _process_symbol(
    session,
    symbol: str,
    quotes: dict,
    secondary: dict[str, float],
    stats_map: dict[str, SymbolStatistics],
    actions: dict[str, CorporateAction],
    session_keys: set[str],
    session_date: date,
    counts: CycleCounts,
) -> list[tuple[str, int, ChangeEvent]]:
    quote = quotes.get(symbol)
    if quote is None:
        # Omitted by the provider: an outage, or simply no data. The last
        # cached quote stays put and ages into STALE on read, which is the
        # honest rendering.
        return []

    now = provider.now()
    stats = stats_map[symbol]
    last_accepted = cache.get_quote(symbol)

    result = validate_tick(quote, last_accepted, stats.std_ret_30d, now)
    if not result.ok:
        log.warning("%s: tick rejected (%s)", symbol, result.rejected_reason)
        counts.rejected += 1
        return []

    result = reconcile(result.accepted, secondary.get(symbol))
    if result.conflict_note:
        log.warning("%s: %s", symbol, result.conflict_note)

    accepted = result.accepted
    counts.accepted += 1

    # Read before overwriting: previous_close is yesterday's close and is more
    # stable in the cached quote than in a fresh one from a wobbling feed.
    previous_close = (
        last_accepted.previous_close if last_accepted else accepted.previous_close
    )

    events: list[ChangeEvent] = []
    action = actions.get(symbol)
    if action is not None:
        # Start from the feed's own value rather than the cached one. The feed
        # always reports previous_close in old shares, so re-deriving from it
        # each cycle is idempotent; carrying the already-adjusted cached value
        # forward would divide it by the ratio again on every single poll.
        previous_close = accepted.previous_close
        stats, previous_close = _apply_corporate_action(action, stats, previous_close)
        # Cache the adjusted quote, not the raw one. Everything downstream --
        # the digest's change_since_seen above all -- reads previous_close from
        # here, and an unadjusted one makes the row report "down 80%" directly
        # beside "price adjusted, holding unchanged".
        accepted = replace(accepted, previous_close=previous_close)
        events.append(
            ChangeEvent(
                symbol=symbol,
                type=EventType.CORPORATE_ACTION,
                severity=0.6,
                occurred_at=now,
                session_date=session_date,
                explanation=(
                    f"{action.action_type.capitalize()} effective today "
                    f"(ratio {action.ratio:g}). Price adjusted, holding unchanged."
                ),
                payload={"action": action.action_type, "ratio": action.ratio},
            )
        )

    cache.set_quote(symbol, accepted)

    context = TickContext(
        symbol=symbol,
        price=accepted.price,
        previous_close=previous_close,
        open_price=accepted.open,
        volume_so_far=accepted.volume,
        session_fraction=calendar.session_fraction(now),
        index_return=_index_return(now),
        observed_at=now,
        session_date=session_date,
    )
    events.extend(detect(context, stats))

    return _persist(session, symbol, dedupe(events, session_keys), counts)


def _index_return(now: datetime) -> float:
    """Today's index return, or zero if we cannot vouch for it being current.

    The residual -- r_stock minus beta times r_index -- is the product's
    headline signal, and it is only meaningful when both halves describe the
    same moment. An index quote from an hour ago paired with a current stock
    price does not measure how the stock moved against the market; it measures
    how far the clock has drifted, and it does it for every symbol at once.

    That is not hypothetical. Jumping the replay clock to a scenario leaves
    the cached index hours behind for one cycle, and every single stock then
    reports a spurious idiosyncratic move -- the one signal that is supposed
    to mean "this moved differently from everything else".

    Returning 0.0 when the reference is stale collapses the residual back to
    the raw move, which is honest: without a trustworthy market return there
    is nothing to subtract.
    """
    quote = cache.get_quote(INDEX_SYMBOL)
    if quote is None or quote.previous_close <= 0:
        return 0.0
    age = abs((now - quote.as_of).total_seconds())
    if age > settings.stale_after_seconds:
        log.warning(
            "index quote is %.0fs old; scoring without a market reference this cycle",
            age,
        )
        return 0.0
    return (quote.price - quote.previous_close) / quote.previous_close


INDEX_SYMBOL = "^NSEI"


def _cycle() -> None:
    now = provider.now()
    market = calendar.market_state(now)

    if market in (MarketState.CLOSED, MarketState.HOLIDAY):
        _shutdown.wait(calendar.poll_interval(market))
        return

    symbols = cache.hot_set()
    if not symbols:
        # Empty either because nobody watches anything, or because this
        # process restarted and lost the cache. Only the database can tell
        # the difference, so ask it.
        with SessionLocal() as session:
            symbols = _rebuild_hot_set(session)
    if not symbols:
        _shutdown.wait(IDLE_SLEEP_SECONDS)
        return

    # The index is polled whether or not anyone watches it: without it there
    # is no market return to subtract, and every residual collapses to the
    # raw move.
    # Index first, and it is not optional ordering. sorted() puts "^NSEI"
    # after every letter, so left alone the benchmark is priced last and every
    # symbol in the cycle measures its residual against the previous cycle's
    # market.
    symbols = [INDEX_SYMBOL] + [s for s in symbols if s != INDEX_SYMBOL]

    started = time.monotonic()
    session_date = _session_date(now)
    counts = CycleCounts()
    published: list[tuple[str, int, ChangeEvent]] = []

    with SessionLocal() as session:
        stats_map = _load_stats(session, symbols)
        actions = _load_actions(session, symbols, session_date)
        session_keys = _load_session_keys(session, session_date)

        for batch in _chunks(symbols, BATCH_SIZE):
            quotes = provider.fetch(batch)
            secondary = (
                provider.secondary_quotes()
                if hasattr(provider, "secondary_quotes")
                else {}
            )
            for symbol in batch:
                try:
                    published.extend(
                        _process_symbol(
                            session, symbol, quotes, secondary, stats_map,
                            actions, session_keys, session_date, counts,
                        )
                    )
                except Exception as exc:
                    # One symbol, one row. Never the whole cycle.
                    log.warning("symbol %s failed: %s", symbol, exc)
        session.commit()

    # Published only after commit, so a subscriber cannot be told about an
    # event that a rollback then erased, and so the id is real.
    for symbol, event_id, event in published:
        cache.publish(symbol, {"type": "event", "id": event_id, "data": event.to_dict()})

    state.last_poll_at = now
    state.last_poll_symbol_count = len(symbols)
    state.last_poll_rejected_count = counts.rejected

    log.info(
        "cycle: %d symbols, %d accepted, %d rejected, %d events, %d duplicates, %.2fs",
        len(symbols), counts.accepted, counts.rejected,
        counts.emitted, counts.duplicates, time.monotonic() - started,
    )

    if not _shutdown.is_set():
        _shutdown.wait(calendar.poll_interval(market))


def run() -> None:
    """Poll until told to stop.

    Waits on the shutdown event rather than sleeping, so SIGTERM during the
    ten-minute closed-market interval stops the worker immediately instead of
    ten minutes later.
    """
    _install_signal_handlers()
    log.info("ingest worker starting (provider=%s)", provider.name)

    while not _shutdown.is_set():
        try:
            _cycle()
        except Exception as exc:
            log.exception("cycle failed: %s", exc)
            _shutdown.wait(ERROR_BACKOFF_SECONDS)

    log.info("ingest worker stopped")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    run()
