"""Market data provider interface.

Two implementations exist from the first commit: a live one and a replay one.
That is not scaffolding, it is a requirement.

Three reasons the replay provider earns its place:

  1. Free Indian market data endpoints are unreliable and rate limited. A demo
     that depends on one is a demo that can fail for reasons unrelated to the
     work.
  2. NSE trades 09:15-15:30 IST. Any presentation outside that window has no
     live data at all, so a live-only build simply cannot be shown.
  3. Most importantly: the interesting behaviour of this system is how it
     handles splits, stale feeds, bad prints and source disagreement. None of
     those can be *demonstrated* on a live feed, because you cannot ask the
     market to produce a bad tick on cue. Replay makes failure reproducible.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class Freshness(str, Enum):
    LIVE = "live"
    DELAYED = "delayed"  # known-delayed source, honestly labelled
    STALE = "stale"  # last update older than the staleness threshold
    UNAVAILABLE = "unavailable"  # no data at all right now


@dataclass(frozen=True)
class Quote:
    """A single observation of a symbol.

    Every quote carries where it came from and when it was true. The UI renders
    that, always. Showing a cached price as though it were current is the one
    failure mode that permanently costs a market product its credibility: a
    user who acts on a number we implied was live has been actively misled, and
    that is worse than showing them nothing.
    """

    symbol: str
    price: float
    open: float
    previous_close: float
    volume: float
    as_of: datetime  # when this price was true, per the source
    source: str  # which provider produced it
    freshness: Freshness = Freshness.LIVE

    def aged(self, now: datetime, stale_after_seconds: int) -> "Quote":
        """Re-evaluate freshness against the current clock.

        Freshness is not a property of the fetch, it is a property of the
        moment you look at it. A quote that was live when fetched becomes stale
        if the feed then stops. So it is recomputed on read rather than frozen
        at write time.
        """
        if self.freshness is Freshness.UNAVAILABLE:
            return self
        age = (now - self.as_of).total_seconds()
        if age > stale_after_seconds:
            return Quote(**{**self.__dict__, "freshness": Freshness.STALE})
        return self


class MarketDataProvider(ABC):
    """Anything that can answer 'what is this symbol doing right now'."""

    name: str = "base"

    @abstractmethod
    def fetch(self, symbols: list[str]) -> dict[str, Quote]:
        """Fetch quotes for a batch of symbols.

        Batched rather than per-symbol because every real upstream charges by
        the request. Implementations must not raise on a partial failure:
        return what succeeded and omit the rest. A single dead symbol must not
        take down the poll cycle for the other two thousand.
        """

    def now(self) -> datetime:
        """The provider's notion of the current time.

        Live providers return wall clock. The replay provider returns its
        virtual clock, which lets a whole trading session be fast-forwarded in
        seconds. Nothing downstream calls datetime.now() directly, so the
        entire system can be run against recorded time without modification.
        """
        return datetime.now()
