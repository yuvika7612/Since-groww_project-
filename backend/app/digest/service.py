"""Digest assembly.

This is the per-user half of the system. It performs no market computation: it
joins precomputed symbol events against this user's read watermarks, then
decides what deserves their attention.

Three ideas do the work here, in order of how much they change the output:

  1. Diff against what *you* last saw, not against yesterday's close.
  2. Collapse moves the market already explains, so twelve red rows saying the
     same thing become one line plus the exceptions.
  3. Rank into a fixed attention budget rather than firing on thresholds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.config import settings
from app.detect.events import MARKET_EVENTS, ChangeEvent, EventType
from app.providers.base import Freshness, Quote


@dataclass
class MarketContext:
    """What the market as a whole did, over the user's absence window."""

    index_symbol: str
    index_return: float
    breadth: float  # share of the user's symbols moving with the index

    @property
    def is_market_wide(self) -> bool:
        """True when the session was dominated by a single market-wide move.

        The threshold pair matters: a large index move with poor breadth is a
        few heavyweights dragging the index, not a market-wide event, and the
        individual rows really are the story in that case.
        """
        return abs(self.index_return) >= 0.008 and self.breadth >= 0.6

    def headline(self) -> str:
        direction = "up" if self.index_return > 0 else "down"
        return (
            f"Market {direction} {abs(self.index_return) * 100:.1f}%. "
            f"{int(self.breadth * 100)}% of your list moved with it."
        )


@dataclass
class DigestRow:
    symbol: str
    name: str
    price: float
    freshness: Freshness
    as_of: datetime
    # Change measured against the price this user last actually saw, which is
    # what "since I last checked" means. Falls back to the session change for
    # a symbol the user has never viewed.
    change_since_seen: float | None
    seen_at: datetime | None
    events: list[ChangeEvent] = field(default_factory=list)
    data_note: str | None = None

    @property
    def score(self) -> float:
        """Rank by the single strongest reason, not by the count of reasons.

        Summing severities would let a symbol with four mild signals outrank
        one with a single severe signal, which inverts what a user cares
        about. A stock hitting a 52-week low on 5x volume is one story, and it
        should be ranked by how big that story is.
        """
        market_events = [e for e in self.events if e.type in MARKET_EVENTS]
        if not market_events:
            return 0.0
        strongest = max(e.severity for e in market_events)
        # Small bonus for corroboration: independent signals agreeing raises
        # confidence, but cannot promote a mild event above a severe one.
        corroboration = min(len(market_events) - 1, 3) * 0.03
        return min(strongest + corroboration, 1.0)

    def primary_reason(self) -> str | None:
        market_events = [e for e in self.events if e.type in MARKET_EVENTS]
        if not market_events:
            return None
        return max(market_events, key=lambda e: e.severity).explanation


@dataclass
class Digest:
    generated_at: datetime
    market: MarketContext | None
    needs_attention: list[DigestRow]
    quiet: list[DigestRow]
    degraded: list[DigestRow]

    @property
    def quiet_summary(self) -> str:
        """The sentence no other watchlist will show.

        Reporting that nothing happened is a real answer, and delivering it
        confidently is the product's main job. Most sessions, for most
        symbols, nothing happened.
        """
        n = len(self.quiet)
        if n == 0:
            return ""
        return f"{n} other {'symbol' if n == 1 else 'symbols'}: nothing meaningful."


def collapse_correlated(
    rows: list[DigestRow], market: MarketContext | None
) -> list[DigestRow]:
    """Suppress rows whose move the market already explains.

    If the index fell 2% and a beta-1 stock fell 2%, that row carries no
    information the market headline has not already delivered. Rendering it as
    an alert spends the user's attention on a duplicate.

    A row survives if it produced an idiosyncratic event, meaning it moved
    differently from what its beta predicted, or if something happened to it
    that has nothing to do with price direction at all: a corporate action, a
    range break, a volume spike.
    """
    if market is None or not market.is_market_wide:
        return rows

    survivors = []
    for row in rows:
        types = {e.type for e in row.events}
        market_independent = types & {
            EventType.IDIOSYNCRATIC_MOVE,
            EventType.CORPORATE_ACTION,
            EventType.RANGE_BREAK,
            EventType.VOLUME_SPIKE,
            EventType.CROSSED_COST_BASIS,
        }
        if market_independent:
            survivors.append(row)
    return survivors


def apply_attention_budget(
    rows: list[DigestRow], budget: int | None = None
) -> tuple[list[DigestRow], list[DigestRow]]:
    """Split rows into a fixed-size attention list and everything else.

    Threshold alerting fails in both directions: on a crash day a 5% rule
    fires on everything at once, and through a calm month it fires on nothing
    while real relative moves go unreported.

    Market volatility varies enormously. A user's attention does not. So the
    constant in this system is the number of things we are willing to ask them
    to look at, and the bar floats to fill it. On a quiet day a 1.2 sigma move
    earns a slot; on a violent one it would not come close.
    """
    budget = budget or settings.attention_budget
    ranked = sorted(rows, key=lambda r: r.score, reverse=True)
    surfaced = [r for r in ranked if r.score > 0][:budget]
    surfaced_symbols = {r.symbol for r in surfaced}
    rest = [r for r in ranked if r.symbol not in surfaced_symbols]
    return surfaced, rest


def build_digest(
    rows: list[DigestRow],
    market: MarketContext | None,
    now: datetime,
    budget: int | None = None,
) -> Digest:
    """Assemble the final digest from per-symbol rows.

    Degraded rows are separated out before ranking. A symbol whose feed has
    failed has not necessarily done anything interesting, and it must never be
    ranked against symbols we have good data for, because its silence is our
    problem rather than the market's.
    """
    degraded = [
        r for r in rows
        if r.freshness in (Freshness.STALE, Freshness.UNAVAILABLE) or r.data_note
    ]
    degraded_symbols = {r.symbol for r in degraded}
    healthy = [r for r in rows if r.symbol not in degraded_symbols]

    candidates = collapse_correlated(healthy, market)
    surfaced, rest = apply_attention_budget(candidates, budget)

    # Rows dropped by the correlation collapse are quiet, not missing: the
    # user can still expand and see them.
    collapsed_symbols = {r.symbol for r in candidates}
    quiet = rest + [r for r in healthy if r.symbol not in collapsed_symbols]

    return Digest(
        generated_at=now,
        market=market,
        needs_attention=surfaced,
        quiet=quiet,
        degraded=degraded,
    )


def compute_market_context(
    index_symbol: str,
    index_return: float,
    rows: list[DigestRow],
) -> MarketContext:
    """Measure how much of this user's list simply followed the index."""
    if not rows or index_return == 0:
        return MarketContext(index_symbol, index_return, breadth=0.0)

    same_direction = 0
    counted = 0
    for row in rows:
        if row.change_since_seen is None:
            continue
        counted += 1
        if row.change_since_seen * index_return > 0:
            same_direction += 1

    breadth = same_direction / counted if counted else 0.0
    return MarketContext(index_symbol, index_return, breadth)
