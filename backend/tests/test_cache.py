"""Tests for the in-process cache.

Only InMemoryCache is exercised here, deliberately. RedisCache needs a server,
and a version of these tests with a mocked Redis client would assert that the
mock behaves like the mock -- it would pass whether or not the real
implementation is correct. The Redis path is covered by integration, not here.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.cache import InMemoryCache
from app.providers.base import Freshness, Quote

AS_OF = datetime(2026, 9, 4, 14, 5)


@pytest.fixture
def cache() -> InMemoryCache:
    """A fresh instance per test: the module singleton would leak state."""
    return InMemoryCache()


def quote(symbol: str, price: float) -> Quote:
    return Quote(
        symbol=symbol,
        price=price,
        open=price * 0.99,
        previous_close=price * 0.98,
        volume=1_000_000.0,
        as_of=AS_OF,
        source="replay",
        freshness=Freshness.DELAYED,
    )


def test_set_quote_then_get_quote_round_trips(cache: InMemoryCache):
    original = quote("TCS", 3900.0)
    cache.set_quote("TCS", original)

    stored = cache.get_quote("TCS")

    assert stored == original
    # Freshness and as_of must survive intact: every staleness decision
    # downstream is made from them.
    assert stored.freshness is Freshness.DELAYED
    assert stored.as_of == AS_OF
    assert stored.source == "replay"


def test_get_quote_returns_none_for_an_unknown_symbol(cache: InMemoryCache):
    """A symbol we have never polled is absent, not an error.

    The poller asks for whatever is in the hot set, and a symbol added a
    moment ago has no quote yet. That is an ordinary state.
    """
    assert cache.get_quote("NOSUCH") is None


def test_get_quotes_returns_every_known_symbol_and_skips_the_rest(cache: InMemoryCache):
    cache.set_quote("TCS", quote("TCS", 3900.0))
    cache.set_quote("INFY", quote("INFY", 1720.0))

    found = cache.get_quotes(["TCS", "INFY", "NOSUCH"])

    assert sorted(found) == ["INFY", "TCS"]
    assert found["TCS"].price == 3900.0
    assert cache.get_quotes([]) == {}


def test_get_quotes_is_batched_rather_than_one_lookup_per_symbol():
    """Guards the rule that makes the digest cheap.

    A user with fifty symbols must cost one operation. If someone later
    reimplements get_quotes as a loop over get_quote, the Redis version
    becomes fifty network round trips on the critical path of every digest
    request. This test fails the moment that shape is introduced.
    """

    class SpyCache(InMemoryCache):
        def __init__(self) -> None:
            super().__init__()
            self.get_quote_calls = 0

        def get_quote(self, symbol: str) -> Quote | None:
            self.get_quote_calls += 1
            return super().get_quote(symbol)

    spy = SpyCache()
    for symbol in ("TCS", "INFY", "RELIANCE"):
        spy.set_quote(symbol, quote(symbol, 100.0))

    found = spy.get_quotes(["TCS", "INFY", "RELIANCE"])

    assert len(found) == 3
    assert spy.get_quote_calls == 0


def test_a_symbol_two_users_watch_survives_one_of_them_dropping_it(cache: InMemoryCache):
    """Refcounting, stated as the behaviour it exists for.

    Two users watch TCS. One removes it. The poller must keep polling it,
    because the other user is still watching.
    """
    assert cache.add_to_hot_set("TCS") == 1
    assert cache.add_to_hot_set("TCS") == 2

    assert cache.remove_from_hot_set("TCS") == 1
    assert cache.hot_set() == ["TCS"]


def test_the_last_user_dropping_a_symbol_takes_it_out_of_the_hot_set(cache: InMemoryCache):
    cache.add_to_hot_set("TCS")
    cache.add_to_hot_set("TCS")
    cache.remove_from_hot_set("TCS")

    assert cache.remove_from_hot_set("TCS") == 0
    assert cache.hot_set() == []


def test_releasing_more_times_than_acquired_does_not_go_negative(cache: InMemoryCache):
    """Otherwise a symbol would need several adds before it polled again."""
    cache.add_to_hot_set("TCS")
    cache.remove_from_hot_set("TCS")

    assert cache.remove_from_hot_set("TCS") == 0
    assert cache.remove_from_hot_set("NEVERSEEN") == 0
    assert cache.hot_set() == []
