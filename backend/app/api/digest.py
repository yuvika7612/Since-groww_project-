"""The digest, the read watermark, and the event stream.

These are the two endpoints the product is actually about, plus the push
channel that keeps them current.

Nothing here recomputes anything. Ranking, correlation collapse, sigma
normalisation and the cost-basis crossing all happen inside the assembler,
once, and this module serialises the result. If a number appears here that
was not computed upstream, the shared/personal split has been broken.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.cache import cache
from app.db import get_session
from app.digest.assembler import assemble_digest
from app.digest.seen import SeenEntry, mark_seen
from app.digest.service import Digest, DigestRow
from app.market.calendar import market_state
from app.models import ChangeEventRow, User, Watchlist, WatchlistItem
from app.providers.base import Freshness
from app.providers.factory import provider
from app.schemas import (
    DigestOut,
    DigestRowOut,
    EventOut,
    MarketOut,
    SeenRequest,
    SeenResponse,
    ist,
)

log = logging.getLogger(__name__)
router = APIRouter(tags=["digest"])

# Long enough not to be chatty, short enough to beat the idle timeout on
# every proxy that matters.
HEARTBEAT_SECONDS = 15

# A client that cannot keep up gets a gap, not backpressure into the poller.
STREAM_BUFFER = 500


def _watched_symbols(session: Session, user_id: int) -> set[str]:
    """Every symbol this user watches, across all their watchlists. One query."""
    return set(
        session.scalars(
            select(WatchlistItem.symbol)
            .join(Watchlist, Watchlist.id == WatchlistItem.watchlist_id)
            .where(Watchlist.user_id == user_id)
            .distinct()
        )
    )


def _row_out(row: DigestRow, source: str | None) -> DigestRowOut:
    unavailable = row.freshness is Freshness.UNAVAILABLE
    return DigestRowOut(
        symbol=row.symbol,
        name=row.name,
        # The assembler carries 0.0 for an unavailable row because DigestRow
        # requires a float. It must not leave the process as a price.
        price=None if unavailable else row.price,
        as_of=None if unavailable else ist(row.as_of),
        source=None if unavailable else source,
        freshness=row.freshness.value,
        change_since_seen=row.change_since_seen,
        seen_at=ist(row.seen_at),
        score=row.score,
        primary_reason=row.primary_reason(),
        events=[
            EventOut(
                type=event.type.value,
                severity=event.severity,
                explanation=event.explanation,
                payload=event.payload,
                occurred_at=ist(event.occurred_at),
            )
            for event in row.events
        ],
        data_note=row.data_note,
    )


def _digest_out(digest: Digest, now: datetime) -> DigestOut:
    every_row = digest.needs_attention + digest.quiet + digest.degraded

    # One batched read purely to attribute each price to its feed. DigestRow
    # does not carry `source`, and the honest fix is adding it there rather
    # than re-reading here; that file is out of scope for this phase.
    sources = {
        symbol: quote.source
        for symbol, quote in cache.get_quotes([r.symbol for r in every_row]).items()
    }

    return DigestOut(
        generated_at=ist(digest.generated_at),
        market_state=market_state(now).value,
        market=(
            MarketOut(
                index_symbol=digest.market.index_symbol,
                index_return=digest.market.index_return,
                breadth=digest.market.breadth,
                is_market_wide=digest.market.is_market_wide,
                headline=digest.market.headline(),
            )
            if digest.market
            else None
        ),
        needs_attention=[_row_out(r, sources.get(r.symbol)) for r in digest.needs_attention],
        quiet=[_row_out(r, sources.get(r.symbol)) for r in digest.quiet],
        quiet_summary=digest.quiet_summary,
        degraded=[_row_out(r, sources.get(r.symbol)) for r in digest.degraded],
    )


@router.get("/digest", response_model=DigestOut)
def get_digest(
    watchlist_id: int | None = Query(default=None),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> DigestOut:
    now = provider.now()
    digest = assemble_digest(session, cache, user.id, watchlist_id, now)
    return _digest_out(digest, now)


@router.post("/seen", response_model=SeenResponse)
def post_seen(
    payload: SeenRequest,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> SeenResponse:
    """Advance watermarks for symbols this user actually watches.

    Unknown symbols are reported back rather than silently dropped or allowed
    to fail the whole batch. Silently dropping leaves a client that believes
    it has marked a row read when it has not, and it has no way to find out.
    Failing the batch punishes eleven good rows for one bad one.
    """
    now = provider.now()
    watched = _watched_symbols(session, user.id)

    accepted: list[SeenEntry] = []
    rejected: list[str] = []
    for entry in payload.entries:
        symbol = entry.symbol.strip().upper()
        if symbol not in watched:
            rejected.append(symbol)
            continue

        seen_at = entry.seen_at
        if seen_at.tzinfo is not None:
            seen_at = ist(seen_at).replace(tzinfo=None)
        # A clock ahead of the server cannot be a real observation.
        seen_at = min(seen_at, now)
        accepted.append(SeenEntry(symbol=symbol, seen_at=seen_at, price=entry.price))

    updated = mark_seen(session, user.id, accepted, now)
    session.commit()
    return SeenResponse(updated=updated, rejected=sorted(set(rejected)))


# --- SSE --------------------------------------------------------------------


def _frame(payload: dict) -> str:
    kind = payload.get("type", "event")
    body = json.dumps(payload.get("data", payload))
    event_id = payload.get("id")
    # Only replayable messages carry an id. quote and market frames are
    # ephemeral snapshots with no row behind them, and giving them ids from a
    # different namespace would make Last-Event-ID resume from a meaningless
    # offset. A reconnecting client refetches the digest anyway.
    prefix = f"id:{event_id}\n" if event_id is not None else ""
    return f"{prefix}event:{kind}\ndata:{body}\n\n"


def _missed_events(session: Session, symbols: set[str], after_id: int) -> list[dict]:
    if not symbols:
        return []
    rows = session.scalars(
        select(ChangeEventRow)
        .where(ChangeEventRow.symbol.in_(symbols), ChangeEventRow.id > after_id)
        .order_by(ChangeEventRow.id)
        .limit(200)
    ).all()
    return [
        {
            "type": "event",
            "id": row.id,
            "data": {
                "symbol": row.symbol,
                "type": row.event_type,
                "severity": row.severity,
                "explanation": row.explanation,
                "payload": row.payload or {},
                "occurred_at": ist(row.occurred_at).isoformat(),
            },
        }
        for row in rows
    ]


async def _stream(request: Request, symbols: list[str], backlog: list[dict]):
    """Bridge the cache's blocking subscription onto the event loop.

    cache.subscribe() blocks on a queue, so iterating it here would freeze
    every other request in the process. A pump thread drains it into an
    asyncio queue instead.

    The private wake channel is what makes shutdown deterministic. A generator
    being iterated by the pump thread cannot be closed from this one -- Python
    raises "generator already executing" -- so on disconnect we publish to a
    channel only this subscription listens on, the pump returns, and only then
    is close() safe. Without it the pump would sit on a blocking get forever
    and the subscriber entry would leak on every reconnect.
    """
    loop = asyncio.get_running_loop()
    inbox: asyncio.Queue = asyncio.Queue(maxsize=STREAM_BUFFER)
    wake_channel = f"__stream:{uuid.uuid4()}"
    subscription = cache.subscribe(symbols + [wake_channel])
    stop = threading.Event()

    def pump() -> None:
        for payload in subscription:
            if stop.is_set() or payload.get("type") == "__close":
                break
            try:
                loop.call_soon_threadsafe(inbox.put_nowait, payload)
            except RuntimeError:
                break  # loop already closed

    worker = threading.Thread(target=pump, name="sse-pump", daemon=True)
    worker.start()

    try:
        for missed in backlog:
            yield _frame(missed)
        # Tells the browser how long to wait before reconnecting.
        yield "retry:3000\n\n"

        while True:
            if await request.is_disconnected():
                break
            try:
                payload = await asyncio.wait_for(
                    inbox.get(), timeout=HEARTBEAT_SECONDS
                )
            except asyncio.TimeoutError:
                yield ": ping\n\n"
                continue
            yield _frame(payload)
    finally:
        stop.set()
        cache.publish(wake_channel, {"type": "__close"})
        worker.join(timeout=2.0)
        subscription.close()


@router.get("/stream")
async def stream(
    request: Request,
    symbols: str = Query(default=""),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> StreamingResponse:
    requested = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    watched = _watched_symbols(session, user.id)
    # The query string is client input. Anything the user does not watch is
    # dropped rather than refused: a stale tab asking for a symbol they just
    # removed should degrade, not error.
    allowed = sorted(set(requested) & watched) if requested else sorted(watched)

    last_event_id = request.headers.get("Last-Event-ID")
    backlog: list[dict] = []
    if last_event_id and last_event_id.isdigit():
        backlog = _missed_events(session, set(allowed), int(last_event_id))

    return StreamingResponse(
        _stream(request, allowed, backlog),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # nginx buffers proxied responses by default, which holds SSE
            # frames until the buffer fills. The stream then works perfectly
            # in dev and silently never arrives in production.
            "X-Accel-Buffering": "no",
        },
    )
