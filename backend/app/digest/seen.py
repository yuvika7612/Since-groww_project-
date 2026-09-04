"""The read watermark: recording what a user has actually seen.

This is the single most important correctness property in the product. Every
other claim is built on it. If a watermark can move backwards, then "since you
last checked" is measured from a moment the user has already read past, and
the diff the whole system exists to compute is a lie.

So the advance is monotonic, and it is monotonic *in the database*, not in
application code. Two tabs flushing concurrently, a retried request, and a
stale tab that has been open since morning all resolve to the same answer
because the upsert itself takes the later of the two timestamps. Read-then-
write in Python would lose that race.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.models import UserSymbolSeen

# A client whose clock is a little fast has done nothing wrong, and its
# reading is still essentially true. Beyond this, the timestamp is not
# plausible as an observation and is replaced with server time rather than
# rejected -- refusing the write would leave the row unread forever.
MAX_CLOCK_SKEW = timedelta(seconds=60)


@dataclass(frozen=True)
class SeenEntry:
    """One row the client reports the user actually looked at.

    price is what was rendered on their screen. The server cannot reconstruct
    it: by the time the flush arrives the price has moved, and the whole point
    is to diff against the number the user actually saw.
    """

    symbol: str
    seen_at: datetime
    price: float | None = None


def _collapse(entries: list[SeenEntry], now: datetime) -> dict[str, SeenEntry]:
    """One entry per symbol, the latest, with future timestamps clamped.

    A debounced client batch can legitimately contain the same symbol twice --
    scrolled past, scrolled back. Both Postgres and SQLite refuse to let a
    single INSERT ... ON CONFLICT touch the same row twice, so the collapse
    has to happen before the statement is built, not after.
    """
    latest: dict[str, SeenEntry] = {}
    for entry in entries:
        seen_at = entry.seen_at
        if seen_at > now + MAX_CLOCK_SKEW:
            seen_at = now
        candidate = SeenEntry(entry.symbol, seen_at, entry.price)

        existing = latest.get(entry.symbol)
        if existing is None or candidate.seen_at > existing.seen_at:
            latest[entry.symbol] = candidate
    return latest


def _insert_for(session: Session):
    """Dialect-specific INSERT with the right two-argument max function.

    Postgres spells it GREATEST; SQLite spells it MAX. Everything else about
    the statement is identical, so this is the only thing that varies.
    """
    if session.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert

        return insert, func.greatest

    from sqlalchemy.dialects.sqlite import insert

    return insert, func.max


def mark_seen(
    session: Session,
    user_id: int,
    entries: list[SeenEntry],
    now: datetime,
) -> int:
    """Advance this user's watermarks. Returns the number of symbols written.

    Does not commit: the caller owns the transaction boundary.

    Two guarantees, both enforced by the statement rather than by control flow:

      last_seen_at never decreases. A late batch from a tab that has been open
      since morning cannot resurrect rows the user has already read elsewhere.

      last_seen_price only changes when last_seen_at actually advances. This
      is the subtle one: without the CASE, a stale batch would leave the newer
      timestamp intact but overwrite the price with an older number, and every
      subsequent diff would be computed against a price the user never saw at
      that moment.
    """
    if not entries:
        return 0

    latest = _collapse(entries, now)
    if not latest:
        return 0

    insert, greatest = _insert_for(session)
    table = UserSymbolSeen.__table__

    statement = insert(table).values(
        [
            {
                "user_id": user_id,
                "symbol": entry.symbol,
                "last_seen_at": entry.seen_at,
                "last_seen_price": entry.price,
            }
            for entry in latest.values()
        ]
    )
    statement = statement.on_conflict_do_update(
        index_elements=["user_id", "symbol"],
        set_={
            "last_seen_at": greatest(
                statement.excluded.last_seen_at, table.c.last_seen_at
            ),
            "last_seen_price": case(
                (
                    statement.excluded.last_seen_at > table.c.last_seen_at,
                    statement.excluded.last_seen_price,
                ),
                else_=table.c.last_seen_price,
            ),
        },
    )

    session.execute(statement)
    return len(latest)
