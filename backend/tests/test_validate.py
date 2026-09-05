"""Tests for tick validation and source reconciliation.

The guiding principle these encode: it is always better to show nothing,
clearly labelled, than a number that is wrong.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.market.validate import reconcile, validate_tick
from app.providers.base import Freshness, Quote

NOW = datetime(2026, 9, 4, 14, 5)


def quote(price: float, as_of: datetime = NOW, source: str = "primary") -> Quote:
    return Quote(
        symbol="INFY",
        price=price,
        open=1720.0,
        previous_close=1720.0,
        volume=1_000_000.0,
        as_of=as_of,
        source=source,
        freshness=Freshness.LIVE,
    )


def test_a_bad_print_is_quarantined_not_discarded():
    """A 42% jump in one tick is a fat finger, not a market event.

    Quarantined rather than dropped: one corroborating tick at a similar
    level and it is accepted next pass, so a genuine limit-up move is delayed
    by a cycle instead of lost.
    """
    result = validate_tick(quote(1720 * 1.42), quote(1720.0), sigma=0.014, now=NOW)

    assert not result.ok
    assert result.accepted is None
    assert "implausible_move" in result.rejected_reason


def test_a_large_move_inside_the_symbols_own_band_is_accepted():
    """The band is proportional to how much this symbol actually moves.

    A fixed percentage would wave nonsense through on a calm large-cap and
    reject legitimate moves on a volatile smallcap.
    """
    # 10% against a 1.4% sigma is 7 sigma: inside the 12-sigma band.
    result = validate_tick(quote(1720 * 1.10), quote(1720.0), sigma=0.014, now=NOW)

    assert result.ok


def test_a_non_positive_price_is_rejected():
    assert not validate_tick(quote(0.0), quote(1720.0), 0.014, NOW).ok
    assert validate_tick(quote(0.0), quote(1720.0), 0.014, NOW).rejected_reason == (
        "non_positive_price"
    )


def test_a_future_timestamp_is_rejected():
    """Clock skew upstream. The price may be fine, but its age is unknowable,
    and age is what every staleness decision depends on."""
    ahead = quote(1725.0, as_of=NOW + timedelta(minutes=5))

    result = validate_tick(ahead, quote(1720.0), sigma=0.014, now=NOW)

    assert not result.ok
    assert result.rejected_reason == "future_timestamp"


def test_sources_disagreeing_serve_the_primary_and_record_the_gap():
    """Never averaged.

    An average of two numbers where one is wrong is a third wrong number that
    matches neither source and destroys the evidence of which feed is broken.
    """
    primary = quote(1720.0)

    result = reconcile(primary, secondary_price=1720.0 * 1.031)

    assert result.ok
    assert result.accepted.price == 1720.0, "primary must be served unchanged"
    assert result.accepted.freshness is Freshness.DELAYED, "disagreement is disclosed"
    assert "Sources disagree" in result.conflict_note


def test_sources_agreeing_within_tolerance_pass_through_untouched():
    primary = quote(1720.0)

    result = reconcile(primary, secondary_price=1720.0 * 1.002)

    assert result.ok
    assert result.accepted is primary
    assert result.conflict_note is None
