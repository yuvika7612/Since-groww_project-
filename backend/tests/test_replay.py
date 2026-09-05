"""Tests for the replay provider and its fault catalogue.

The replay provider is not scaffolding. Every fault here corresponds to a
real failure mode the system claims to handle, and none of them can be
produced on cue from a live market -- which is the whole reason the demo can
show what this system does when things go wrong.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from app.providers.base import Freshness
from app.providers.replay import Fault, ReplayProvider

START = datetime(2026, 9, 4, 10, 0)


@pytest.fixture
def fixture_path(tmp_path):
    """Two symbols, three frames each, in time order."""
    path = tmp_path / "session.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for minute in range(3):
            stamp = (START + timedelta(minutes=minute)).isoformat()
            for symbol, price in (("TCS", 3900.0), ("INFY", 1720.0)):
                handle.write(
                    json.dumps(
                        {
                            "symbol": symbol,
                            "as_of": stamp,
                            "price": price + minute,
                            "open": price,
                            "previous_close": price - 5,
                            "volume": 100_000.0 * (minute + 1),
                        }
                    )
                    + "\n"
                )
    return path


def test_the_virtual_clock_advances_at_the_configured_speed(fixture_path):
    """A 6h15m session has to replay in minutes or the demo cannot be shown."""
    fast = ReplayProvider(fixture_path, speed=60.0, start_at=START)
    slow = ReplayProvider(fixture_path, speed=1.0, start_at=START)

    import time

    time.sleep(0.2)
    fast_elapsed = (fast.now() - START).total_seconds()
    slow_elapsed = (slow.now() - START).total_seconds()

    assert fast_elapsed > slow_elapsed * 10, "speed multiplier is not applied"


def test_seek_moves_the_clock_to_an_exact_moment(fixture_path):
    provider = ReplayProvider(fixture_path, speed=1.0, start_at=START)

    provider.seek(START + timedelta(minutes=2))

    assert abs((provider.now() - (START + timedelta(minutes=2))).total_seconds()) < 1


def test_an_outage_omits_the_symbol_entirely(fixture_path):
    """The consumer must handle a symbol simply not being in the response.

    Omission is honest: the last cached quote then ages into STALE on read
    rather than a fabricated price being served.
    """
    provider = ReplayProvider(fixture_path, speed=1.0, start_at=START)
    provider.schedule(
        Fault(kind="outage", symbol="INFY", start=START, end=START + timedelta(hours=1))
    )

    quotes = provider.fetch(["TCS", "INFY"])

    assert "TCS" in quotes
    assert "INFY" not in quotes


def test_a_bad_tick_multiplies_the_price(fixture_path):
    provider = ReplayProvider(fixture_path, speed=1.0, start_at=START)
    clean = provider.fetch(["TCS"])["TCS"].price
    provider.schedule(
        Fault(kind="bad_tick", symbol="TCS", start=START,
              end=START + timedelta(hours=1), magnitude=1.4)
    )

    dirty = provider.fetch(["TCS"])["TCS"].price

    assert dirty == pytest.approx(clean * 1.4, rel=1e-3)


def test_a_frozen_feed_keeps_responding_but_stops_advancing_as_of(fixture_path):
    """More dangerous than an outage: the payload looks perfectly healthy and
    only the timestamp reveals the problem."""
    provider = ReplayProvider(fixture_path, speed=1.0, start_at=START)
    provider.schedule(
        Fault(kind="frozen", symbol="TCS", start=START, end=START + timedelta(hours=1))
    )

    quote = provider.fetch(["TCS"])["TCS"]

    assert quote.price > 0, "a frozen feed still returns a plausible payload"
    assert quote.as_of == START, "as_of must stop advancing"


def test_a_split_divides_both_the_price_and_the_open(fixture_path):
    """Every price in the quote is in new shares on an ex-date.

    Dividing the last trade alone leaves the opening print in old shares, and
    overnight_gap then compares it against a restated previous_close and
    reports a 400% gap. previous_close is deliberately left raw: a real feed
    does not restate it, and surviving that is the point.
    """
    provider = ReplayProvider(fixture_path, speed=1.0, start_at=START)
    clean = provider.fetch(["TCS"])["TCS"]
    provider.schedule(
        Fault(kind="split", symbol="TCS", start=START,
              end=START + timedelta(hours=1), magnitude=5.0)
    )

    split = provider.fetch(["TCS"])["TCS"]

    assert split.price == pytest.approx(clean.price / 5.0, rel=1e-3)
    assert split.open == pytest.approx(clean.open / 5.0, rel=1e-3)
    assert split.previous_close == clean.previous_close


def test_a_conflict_publishes_a_disagreeing_secondary_price(fixture_path):
    provider = ReplayProvider(fixture_path, speed=1.0, start_at=START)
    assert provider.secondary_quotes() == {}
    provider.schedule(
        Fault(kind="conflict", symbol="TCS", start=START,
              end=START + timedelta(hours=1), magnitude=1.0)
    )

    primary = provider.fetch(["TCS"])["TCS"]
    secondary = provider.secondary_quotes()

    assert "TCS" in secondary
    assert secondary["TCS"] != primary.price
    assert secondary["TCS"] == pytest.approx(primary.price * 1.03, rel=1e-2)


def test_freshness_is_recomputed_against_the_clock_not_frozen_at_fetch(fixture_path):
    """Staleness is a property of the moment you look, not of the fetch."""
    provider = ReplayProvider(fixture_path, speed=1.0, start_at=START)
    quote = provider.fetch(["TCS"])["TCS"]

    assert quote.freshness is Freshness.LIVE
    assert quote.aged(START + timedelta(minutes=10), 90).freshness is Freshness.STALE
