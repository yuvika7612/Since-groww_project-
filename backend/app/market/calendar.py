"""NSE trading calendar.

Everything time-dependent in the system routes through here: session state,
the volume profile relative_volume() needs, and the previous_close each
return is computed against. Centralising it means a holiday fix or a
session-hours change happens in one file instead of being reimplemented
slightly differently by the poller and the nightly job.

Nothing here calls datetime.now(). Every function takes `when` explicitly, so
detection runs identically against the replay clock and the wall clock -- see
providers/base.py:MarketDataProvider.now().
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from enum import Enum

from app.config import settings

try:
    import pandas_market_calendars as mcal  # optional; used only if already installed
except ImportError:  # pragma: no cover - exercised only when the package is present
    mcal = None


class MarketState(str, Enum):
    CLOSED = "closed"
    PRE_OPEN = "pre_open"      # 09:00-09:15, auction, prices are indicative
    OPEN = "open"              # 09:15-15:30
    POST_CLOSE = "post_close"  # 15:30-16:00, closing session
    HOLIDAY = "holiday"


_PRE_OPEN_START = time(9, 0)
_OPEN = time(9, 15)
_CLOSE = time(15, 30)
_POST_CLOSE_END = time(16, 0)

# Public because the live provider needs it too: Yahoo returns tz-aware
# timestamps and the rest of this system speaks naive IST. India has no DST,
# so a fixed offset is exactly correct rather than merely convenient.
IST = timezone(timedelta(hours=5, minutes=30))

# NSE trading holidays for 2026. Hardcoded rather than fetched, because the
# exchange publishes this list once a year, so a live lookup would just be one
# more thing that can be unavailable during a demo for no benefit.
#
# Fixed-date civil holidays (Republic Day, Independence Day, Gandhi Jayanti,
# Christmas) are certain. Lunar-calendar holidays (Holi, Ram Navami, Ganesh
# Chaturthi, Dussehra, Diwali, Guru Nanak Jayanti, ...) are estimated from the
# corresponding Hindu calendar and have NOT been checked against NSE's
# official circular -- production use requires confirming these against the
# exchange's published list, which typically comes out in December of the
# prior year.
NSE_HOLIDAYS_2026: frozenset[date] = frozenset({
    date(2026, 1, 26),   # Republic Day
    date(2026, 3, 4),    # Holi
    date(2026, 3, 26),   # Ram Navami (estimated)
    date(2026, 4, 3),    # Good Friday
    date(2026, 4, 14),   # Ambedkar Jayanti
    date(2026, 5, 1),    # Maharashtra Day
    date(2026, 8, 28),   # Ganesh Chaturthi (estimated)
    date(2026, 10, 2),   # Gandhi Jayanti / Dussehra
    date(2026, 11, 9),   # Diwali Laxmi Pujan (estimated)
    date(2026, 11, 10),  # Diwali Balipratipada (estimated)
    date(2026, 11, 24),  # Guru Nanak Jayanti (estimated)
    date(2026, 12, 25),  # Christmas
})


def _mcal_holidays_2026() -> frozenset[date] | None:
    """Trading holidays for 2026 from pandas_market_calendars, if installed.

    Not a hard dependency -- the project runs with zero infrastructure by
    design (see config.py) -- but if a maintained calendar happens to be on
    the path, prefer it over the hardcoded, partly-estimated list above.
    """
    if mcal is None:
        return None
    try:
        exchange = mcal.get_calendar("NSE")
        schedule = exchange.schedule(start_date="2026-01-01", end_date="2026-12-31")
        trading_days = {ts.date() for ts in schedule.index}
    except Exception:
        # Wrong calendar name for this package version, or any other
        # integration hiccup: fall back rather than break the whole system
        # over an optional convenience.
        return None
    all_days = {date(2026, 1, 1) + timedelta(days=i) for i in range(365)}
    weekdays = {d for d in all_days if d.weekday() < 5}
    return frozenset(weekdays - trading_days)


# Computed once at import time, not per call: the calendar does not change
# while the process is running. An empty result from the package is treated as
# a failure rather than as "2026 has no holidays", which it certainly does.
_mcal_result = _mcal_holidays_2026()
_RESOLVED_HOLIDAYS_2026 = _mcal_result if _mcal_result else NSE_HOLIDAYS_2026


def _as_ist_naive(when: datetime) -> datetime:
    """Normalise to a naive IST wall-clock datetime.

    The rest of the system already treats naive datetimes as IST wall-clock
    (see providers/base.py and ReplayProvider's virtual clock), so a naive
    input is trusted as-is. An aware input is converted, so a caller that
    passes UTC by mistake still lands on the correct session boundary instead
    of silently being off by 5.5 hours.
    """
    if when.tzinfo is not None:
        return when.astimezone(IST).replace(tzinfo=None)
    return when


def is_trading_day(d: date) -> bool:
    if d.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    return d not in _RESOLVED_HOLIDAYS_2026


def previous_trading_day(d: date) -> date:
    """The most recent trading day strictly before d.

    Must walk back over weekends and holidays together, not just one: the day
    before a Monday holiday is not Sunday, it is the Friday before it. Getting
    this wrong silently corrupts previous_close, and therefore every return
    computed from it, on the day after every long weekend.
    """
    candidate = d - timedelta(days=1)
    while not is_trading_day(candidate):
        candidate -= timedelta(days=1)
    return candidate


def market_state(when: datetime) -> MarketState:
    """Which session phase `when` falls in, in IST."""
    ist = _as_ist_naive(when)
    if not is_trading_day(ist.date()):
        return MarketState.HOLIDAY

    t = ist.time()
    if t < _PRE_OPEN_START:
        return MarketState.CLOSED
    if t < _OPEN:
        return MarketState.PRE_OPEN
    if t < _CLOSE:
        return MarketState.OPEN
    if t < _POST_CLOSE_END:
        return MarketState.POST_CLOSE
    return MarketState.CLOSED


def poll_interval(state: MarketState) -> int:
    """Poll cadence in seconds for a given market state.

    Costing almost nothing overnight is the point: OPEN polls every 5s,
    PRE_OPEN every 30s (indicative auction prices, lower urgency), and
    everything else -- closed, post-close, holiday -- backs off to the
    10-minute floor. There is nothing to detect outside a session, so there
    is no reason to poll as if there were.
    """
    if state is MarketState.OPEN:
        return settings.poll_interval_open
    if state is MarketState.PRE_OPEN:
        return settings.poll_interval_preopen
    return settings.poll_interval_closed


# --- Intraday volume profile ------------------------------------------------
#
# Intraday volume is U-shaped: heavy at the open, thin through midday, heavy
# into the close. relative_volume() in detect/signals.py divides today's
# volume-so-far by this expected fraction; without it, 10:15am would be
# compared against a full day's average and read as a volume drought every
# single morning, regardless of what actually happened.
#
# TODO: this is a static, hand-shaped approximation of the NSE-wide profile.
# The honest version computes it per symbol from historical intraday bars --
# a large-cap and a thinly-traded smallcap do not open and close in the same
# shape. The fixed table is enough to make relative_volume() correct; a
# per-symbol profile is real future work, not urgent enough to block this.
_VOLUME_PROFILE: tuple[tuple[time, float], ...] = (
    (time(9, 15), 0.00), (time(9, 30), 0.11), (time(10, 0), 0.26), (time(10, 30), 0.35),
    (time(11, 0), 0.42), (time(11, 30), 0.48), (time(12, 0), 0.53), (time(12, 30), 0.56),
    (time(13, 0), 0.60), (time(13, 30), 0.66), (time(14, 0), 0.72), (time(14, 30), 0.78),
    (time(15, 0), 0.86), (time(15, 15), 0.93), (time(15, 30), 1.00),
)


def _seconds_since_midnight(t: time) -> int:
    return t.hour * 3600 + t.minute * 60 + t.second


def session_fraction(when: datetime) -> float:
    """Expected share of a normal day's volume traded by this point.

    0.0 before the open, 1.0 at and after the close, linearly interpolated
    between the 15-minute buckets above in between.
    """
    ist = _as_ist_naive(when)
    t = ist.time()

    if t <= _VOLUME_PROFILE[0][0]:
        return 0.0
    if t >= _VOLUME_PROFILE[-1][0]:
        return 1.0

    for (t0, f0), (t1, f1) in zip(_VOLUME_PROFILE, _VOLUME_PROFILE[1:]):
        if t0 <= t <= t1:
            span = _seconds_since_midnight(t1) - _seconds_since_midnight(t0)
            if span <= 0:
                return f0
            progress = (_seconds_since_midnight(t) - _seconds_since_midnight(t0)) / span
            return f0 + progress * (f1 - f0)

    return 1.0  # unreachable: the loop above covers [first, last] exhaustively
