"""Pure signal math.

Every function here is a pure function of numbers. No database, no clock, no
network. That is deliberate: these are the only functions in the system whose
correctness is hard to eyeball, so they are the ones that get unit tested.

The central idea: a raw percentage move is meaningless on its own. Four percent
is a routine session for a smallcap and a significant event for a large-cap
bank. Every signal below expresses a move in units of that symbol's own normal
behaviour, not in absolute percent.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Below this, a symbol's return distribution is degenerate: it is suspended,
# illiquid, or the feed has flat-lined. Dividing by it produces a huge z-score
# that would dominate the digest, so we refuse to score instead.
MIN_SIGMA = 1e-6


@dataclass(frozen=True)
class SymbolStatistics:
    """Trailing statistics for one symbol, recomputed nightly.

    Kept as a frozen dataclass rather than an ORM row so the signal functions
    stay independent of the persistence layer.
    """

    mean_ret_30d: float
    std_ret_30d: float
    avg_vol_20d: float
    high_52w: float
    low_52w: float
    beta_60d: float = 1.0

    @property
    def is_scorable(self) -> bool:
        return self.std_ret_30d > MIN_SIGMA and self.avg_vol_20d > 0


def z_score(today_return: float, stats: SymbolStatistics) -> float:
    """How unusual today's move is for this specific symbol.

    Returns the move in standard deviations of the symbol's own 30-day return
    distribution. A +4% day is z=4.0 for a stock that usually moves 1%, and
    z=0.67 for one that usually moves 6%.

    Returns 0.0 for degenerate distributions rather than raising, because a
    suspended symbol should silently drop out of ranking, not crash ingestion.
    """
    if not stats.is_scorable:
        return 0.0
    return (today_return - stats.mean_ret_30d) / stats.std_ret_30d


def relative_volume(
    volume_so_far: float, stats: SymbolStatistics, session_fraction: float
) -> float:
    """Today's participation versus normal, adjusted for time of day.

    Intraday volume is U-shaped: heavy at the open, thin through midday, heavy
    into the close. Comparing 10:15am volume against a full-day average would
    understate it roughly fivefold and make every morning look dead. So we
    compare against the volume we would *expect* to have seen by now.

    session_fraction is the expected share of a normal day's volume traded by
    this point in the session (see market/calendar.py).
    """
    if not stats.is_scorable:
        return 1.0
    expected = stats.avg_vol_20d * max(session_fraction, 0.01)
    if expected <= 0:
        return 1.0
    return volume_so_far / expected


def residual_return(
    stock_return: float, index_return: float, stats: SymbolStatistics
) -> float:
    """The part of today's move that the market does not explain.

    This is the signal that separates a useful watchlist from a noisy one.

    If Nifty falls 2% and a stock with beta 1.0 falls 2%, the stock told you
    nothing that the index had not already told you. Its residual is zero and
    it should not consume a slot in the digest. A stock that stayed *flat* on
    that day has a residual of +2% and is genuinely interesting, even though a
    conventional watchlist would render it as an unremarkable grey row.
    """
    return stock_return - stats.beta_60d * index_return


def overnight_gap(open_price: float, previous_close: float) -> float:
    """Fractional gap between yesterday's close and today's open.

    A stock that gapped 4% at the open and then went sideways has reacted to
    information released outside market hours. A stock that ground 4% lower
    through the session is responding to flow. Different causes, different
    meanings, so we detect them separately rather than lumping both into the
    day's return.
    """
    if previous_close <= 0:
        return 0.0
    return (open_price - previous_close) / previous_close


def range_break(price: float, stats: SymbolStatistics) -> float:
    """Signed distance beyond the 52-week range, as a fraction.

    Positive means a new high, negative a new low, zero means inside the range.
    Round-number and range boundaries matter because they are the levels users
    actually anchor on, independent of any statistical significance.
    """
    if price > stats.high_52w > 0:
        return (price - stats.high_52w) / stats.high_52w
    if 0 < price < stats.low_52w:
        return (price - stats.low_52w) / stats.low_52w
    return 0.0


def saturate(value: float, cap: float) -> float:
    """Map an unbounded magnitude onto [0, 1] so severities stay comparable.

    Without this, one 9-sigma print would rank above every other event forever.
    Past the cap we treat everything as equally extreme: the difference between
    a 6-sigma and a 9-sigma move is not something a user acts on differently.
    """
    if cap <= 0:
        return 0.0
    return min(abs(value) / cap, 1.0)


def log_saturate(value: float, cap: float) -> float:
    """Saturation on a log scale, for quantities like volume that span decades.

    3x normal volume is a much bigger step up from 1x than 10x is from 8x, and
    a linear scale would not reflect that.
    """
    if value <= 0 or cap <= 1:
        return 0.0
    return min(math.log(max(value, 1.0)) / math.log(cap), 1.0)
