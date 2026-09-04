"""Shared live state: latest quote per symbol, the hot set, and fan-out.

Two implementations behind one Protocol, chosen by whether `redis_url` is
set. The in-memory one exists so the project runs on a bare clone with no
infrastructure at all, which matters when someone is evaluating it and has
ninety seconds of patience.

What lives here is the *shared* half of the architecture: sized by
instruments, not by users. Nothing per-user belongs in this module.

A note on freshness, because it is a non-negotiable rule elsewhere in the
system: this module stores whatever `Freshness` the provider set and does not
touch it. Staleness is recomputed by the *reader* via `Quote.aged()`, because
it is a property of the moment you look, not of the moment you cached.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from collections import Counter
from collections.abc import Iterator
from datetime import datetime
from typing import Protocol

from app.config import settings
from app.providers.base import Freshness, Quote

log = logging.getLogger(__name__)

HOT_SET_KEY = "hot"
_QUOTE_PREFIX = "quote:"
_CHANNEL_PREFIX = "sym:"

# Long enough that staleness is always decided by as_of rather than by
# eviction: a frozen feed must render as STALE (we have a price, it is old),
# never as UNAVAILABLE (we have nothing). The TTL exists only so quotes for
# symbols nobody watches any more do not accumulate forever.
QUOTE_TTL_SECONDS = 3 * 24 * 3600


class Cache(Protocol):
    def get_quote(self, symbol: str) -> Quote | None: ...

    def set_quote(self, symbol: str, quote: Quote) -> None: ...

    def get_quotes(self, symbols: list[str]) -> dict[str, Quote]: ...

    def add_to_hot_set(self, symbol: str) -> int: ...

    def remove_from_hot_set(self, symbol: str) -> int: ...

    def hot_set(self) -> list[str]: ...

    def publish(self, symbol: str, payload: dict) -> None: ...

    def subscribe(self, symbols: list[str]) -> Iterator[dict]: ...


# --------------------------------------------------------------------------
# Serialisation
# --------------------------------------------------------------------------


def _encode(quote: Quote) -> str:
    return json.dumps(
        {
            "symbol": quote.symbol,
            "price": quote.price,
            "open": quote.open,
            "previous_close": quote.previous_close,
            "volume": quote.volume,
            "as_of": quote.as_of.isoformat(),
            "source": quote.source,
            "freshness": quote.freshness.value,
        }
    )


def _decode(raw: str | bytes) -> Quote:
    data = json.loads(raw)
    return Quote(
        symbol=data["symbol"],
        price=data["price"],
        open=data["open"],
        previous_close=data["previous_close"],
        volume=data["volume"],
        as_of=datetime.fromisoformat(data["as_of"]),
        source=data["source"],
        freshness=Freshness(data["freshness"]),
    )


# --------------------------------------------------------------------------
# Redis
# --------------------------------------------------------------------------

# Decrement-and-maybe-delete has to be one atomic step. Between a ZINCRBY that
# returns 0 and a separate ZREM, another user can add the same symbol back;
# deleting it there would drop a symbol somebody is watching out of the poll
# loop, and their row would quietly stop updating forever.
_RELEASE_SCRIPT = """
local remaining = tonumber(redis.call('ZINCRBY', KEYS[1], -1, ARGV[1]))
if remaining <= 0 then
    redis.call('ZREM', KEYS[1], ARGV[1])
    return 0
end
return remaining
"""


# Untested without a server. Verified correct by inspection against redis-py
# docs. Integration test lives in docker-compose.
class RedisCache:
    """Shared across processes, which is the only reason it exists.

    The API and the poller are separate processes. A hot set that lives in one
    of them is invisible to the other, so a symbol a user just added would
    never be polled. That is the problem this implementation solves.
    """

    def __init__(self, url: str):
        # Imported here rather than at module scope so that a clone without
        # redis-py installed can still run the default in-memory path. The
        # zero-infrastructure promise in the README depends on this.
        import redis

        self._redis = redis.Redis.from_url(url, decode_responses=True)
        self._release = self._redis.register_script(_RELEASE_SCRIPT)

    @staticmethod
    def _quote_key(symbol: str) -> str:
        return f"{_QUOTE_PREFIX}{symbol}"

    @staticmethod
    def _channel(symbol: str) -> str:
        return f"{_CHANNEL_PREFIX}{symbol}"

    def get_quote(self, symbol: str) -> Quote | None:
        raw = self._redis.get(self._quote_key(symbol))
        return _decode(raw) if raw else None

    def set_quote(self, symbol: str, quote: Quote) -> None:
        self._redis.set(self._quote_key(symbol), _encode(quote), ex=QUOTE_TTL_SECONDS)

    def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """One MGET, not one GET per symbol.

        A user with fifty symbols must cost one round trip. Looping here would
        put fifty network hops on the critical path of every digest request,
        which is the N+1 this architecture exists to avoid.
        """
        if not symbols:
            return {}
        raws = self._redis.mget([self._quote_key(s) for s in symbols])
        return {symbol: _decode(raw) for symbol, raw in zip(symbols, raws) if raw}

    def add_to_hot_set(self, symbol: str) -> int:
        return int(self._redis.zincrby(HOT_SET_KEY, 1, symbol))

    def remove_from_hot_set(self, symbol: str) -> int:
        return int(self._release(keys=[HOT_SET_KEY], args=[symbol]))

    def hot_set(self) -> list[str]:
        return list(self._redis.zrange(HOT_SET_KEY, 0, -1))

    def publish(self, symbol: str, payload: dict) -> None:
        self._redis.publish(self._channel(symbol), json.dumps(payload))

    def subscribe(self, symbols: list[str]) -> Iterator[dict]:
        if not symbols:
            return
        pubsub = self._redis.pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe(*[self._channel(s) for s in symbols])
        try:
            for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                yield json.loads(message["data"])
        finally:
            # Without this, a disconnected SSE client leaks a Redis connection
            # per reconnect, and browsers reconnect aggressively.
            pubsub.close()


# --------------------------------------------------------------------------
# In-memory
# --------------------------------------------------------------------------


class InMemoryCache:
    """Per-process fallback so a bare clone runs with no infrastructure.

    IMPORTANT CAVEAT: this is per-process state. The API and the ingestion
    worker each get their *own* copy, so a symbol the API adds to the hot set
    is invisible to the worker, and quotes the worker caches are invisible to
    the API. The two halves genuinely do not talk to each other here.

    That is acceptable for exactly two situations: the test suite, and running
    the API and worker inside one process. Anything else needs Redis. This is
    a deliberate trade so that `pytest` and a fresh clone work with zero setup,
    not an oversight.
    """

    def __init__(self) -> None:
        self._quotes: dict[str, Quote] = {}
        self._hot: Counter[str] = Counter()
        self._subscribers: list[tuple[set[str], queue.Queue]] = []
        # The poller writes while an SSE generator reads; both are threads.
        self._lock = threading.RLock()

    def get_quote(self, symbol: str) -> Quote | None:
        with self._lock:
            return self._quotes.get(symbol)

    def set_quote(self, symbol: str, quote: Quote) -> None:
        with self._lock:
            self._quotes[symbol] = quote

    def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """One pass over the local dict under one lock acquisition.

        There is no round trip to save here, but the shape matches RedisCache
        deliberately: calling get_quote in a loop would take and release the
        lock per symbol, and would set the pattern that the Redis
        implementation must not follow.
        """
        with self._lock:
            return {s: self._quotes[s] for s in symbols if s in self._quotes}

    def add_to_hot_set(self, symbol: str) -> int:
        with self._lock:
            self._hot[symbol] += 1
            return self._hot[symbol]

    def remove_from_hot_set(self, symbol: str) -> int:
        with self._lock:
            if symbol not in self._hot:
                return 0
            self._hot[symbol] -= 1
            if self._hot[symbol] <= 0:
                del self._hot[symbol]
                return 0
            return self._hot[symbol]

    def hot_set(self) -> list[str]:
        # Sorted for determinism in tests. Order is not part of the Cache
        # contract; Redis returns ZRANGE order, which is by refcount then lex.
        with self._lock:
            return sorted(self._hot)

    def publish(self, symbol: str, payload: dict) -> None:
        with self._lock:
            targets = [q for symbols, q in self._subscribers if symbol in symbols]
        for target in targets:
            target.put(payload)

    def subscribe(self, symbols: list[str]) -> Iterator[dict]:
        entry = (set(symbols), queue.Queue())
        with self._lock:
            self._subscribers.append(entry)
        try:
            while True:
                yield entry[1].get()
        finally:
            # Runs when the SSE client disconnects and the generator is closed.
            with self._lock:
                self._subscribers.remove(entry)


# --------------------------------------------------------------------------


def build_cache() -> Cache:
    if settings.redis_url:
        return RedisCache(settings.redis_url)
    log.info("No redis_url configured; using per-process in-memory cache.")
    return InMemoryCache()


cache: Cache = build_cache()
