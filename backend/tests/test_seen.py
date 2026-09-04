"""Tests for the read watermark.

If any of these fail, "since you last checked" is measuring from the wrong
moment and the product's central claim is false. They are written as
statements about user-visible behaviour rather than about the SQL, because
the SQL is allowed to change and the behaviour is not.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from app.digest.seen import SeenEntry, mark_seen
from app.models import UserSymbolSeen
from tests.conftest import make_symbols, make_user

NOW = datetime(2026, 9, 4, 15, 0)
MORNING = datetime(2026, 9, 4, 9, 30)
MIDDAY = datetime(2026, 9, 4, 12, 0)


def watermark(session, user_id: int, symbol: str) -> UserSymbolSeen | None:
    return session.scalars(
        select(UserSymbolSeen).where(
            UserSymbolSeen.user_id == user_id, UserSymbolSeen.symbol == symbol
        )
    ).first()


def setup(session):
    user = make_user(session)
    make_symbols(session, "TCS", "INFY")
    return user


def test_a_first_sighting_is_recorded(session):
    user = setup(session)

    written = mark_seen(session, user.id, [SeenEntry("TCS", MIDDAY, 3900.0)], NOW)

    assert written == 1
    mark = watermark(session, user.id, "TCS")
    assert mark.last_seen_at == MIDDAY
    assert mark.last_seen_price == 3900.0


def test_a_stale_tab_cannot_drag_the_watermark_backwards(session):
    """The case this whole module exists for.

    A tab open since the morning flushes after the user has already read the
    row on their phone at midday. The morning timestamp must lose.
    """
    user = setup(session)
    mark_seen(session, user.id, [SeenEntry("TCS", MIDDAY, 3900.0)], NOW)

    mark_seen(session, user.id, [SeenEntry("TCS", MORNING, 3800.0)], NOW)

    mark = watermark(session, user.id, "TCS")
    assert mark.last_seen_at == MIDDAY


def test_a_stale_batch_does_not_rewrite_the_price_either(session):
    """The subtle half of the same bug.

    Taking the later timestamp but the losing batch's price would leave the
    watermark claiming the user saw 3800 at midday, when they saw 3900. Every
    later diff would then be computed from a price that was never on screen.
    """
    user = setup(session)
    mark_seen(session, user.id, [SeenEntry("TCS", MIDDAY, 3900.0)], NOW)

    mark_seen(session, user.id, [SeenEntry("TCS", MORNING, 3800.0)], NOW)

    mark = watermark(session, user.id, "TCS")
    assert mark.last_seen_price == 3900.0


def test_batches_converge_to_the_latest_regardless_of_arrival_order(session):
    """Out-of-order delivery must not change the outcome."""
    user = setup(session)
    ordered = [
        SeenEntry("TCS", MORNING, 3800.0),
        SeenEntry("TCS", MIDDAY, 3900.0),
    ]

    for batch in (ordered, list(reversed(ordered))):
        session.execute(UserSymbolSeen.__table__.delete())
        for entry in batch:
            mark_seen(session, user.id, [entry], NOW)

        mark = watermark(session, user.id, "TCS")
        assert mark.last_seen_at == MIDDAY
        assert mark.last_seen_price == 3900.0


def test_replaying_an_identical_batch_changes_nothing(session):
    """Retries are normal: the client flushes on a timer and on page hide."""
    user = setup(session)
    batch = [SeenEntry("TCS", MIDDAY, 3900.0), SeenEntry("INFY", MIDDAY, 1720.0)]

    mark_seen(session, user.id, batch, NOW)
    before = {
        (m.symbol, m.last_seen_at, m.last_seen_price)
        for m in session.scalars(select(UserSymbolSeen))
    }

    mark_seen(session, user.id, batch, NOW)
    after = {
        (m.symbol, m.last_seen_at, m.last_seen_price)
        for m in session.scalars(select(UserSymbolSeen))
    }

    assert before == after


def test_a_wildly_future_timestamp_is_clamped_not_rejected(session):
    """A skewed client clock is not the user's fault.

    Rejecting the write would leave the row permanently unread. Clamping to
    server time keeps the watermark plausible and the row readable.
    """
    user = setup(session)
    far_future = NOW + timedelta(hours=3)

    written = mark_seen(session, user.id, [SeenEntry("TCS", far_future, 3900.0)], NOW)

    assert written == 1
    assert watermark(session, user.id, "TCS").last_seen_at == NOW


def test_small_clock_skew_is_tolerated_as_reported(session):
    """Half a minute fast is still a truthful observation."""
    user = setup(session)
    slightly_ahead = NOW + timedelta(seconds=30)

    mark_seen(session, user.id, [SeenEntry("TCS", slightly_ahead, 3900.0)], NOW)

    assert watermark(session, user.id, "TCS").last_seen_at == slightly_ahead


def test_a_symbol_repeated_within_one_batch_collapses_to_the_latest(session):
    """Scrolled past, scrolled back. Both stores refuse a double-touched row."""
    user = setup(session)

    written = mark_seen(
        session,
        user.id,
        [
            SeenEntry("TCS", MORNING, 3800.0),
            SeenEntry("TCS", MIDDAY, 3900.0),
        ],
        NOW,
    )

    assert written == 1
    mark = watermark(session, user.id, "TCS")
    assert mark.last_seen_at == MIDDAY
    assert mark.last_seen_price == 3900.0


def test_watermarks_are_per_user(session):
    """The same event is unread for one user and read for another."""
    user = setup(session)
    other = make_user(session, email="other@example.com")

    mark_seen(session, user.id, [SeenEntry("TCS", MIDDAY, 3900.0)], NOW)

    assert watermark(session, user.id, "TCS") is not None
    assert watermark(session, other.id, "TCS") is None


def test_an_empty_batch_is_a_no_op(session):
    user = setup(session)

    assert mark_seen(session, user.id, [], NOW) == 0
    assert session.scalars(select(UserSymbolSeen)).all() == []
