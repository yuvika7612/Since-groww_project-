"""Tests for the NSE trading calendar.

Two things here are load-bearing rather than cosmetic. previous_trading_day()
resolves the previous_close every return is computed against, so an off-by-one
over a long weekend corrupts every downstream signal silently. And
session_fraction() is the denominator of relative_volume(), so a wrong shape
makes every morning read as a volume drought.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from app.config import settings
from app.market.calendar import (
    MarketState,
    is_trading_day,
    market_state,
    poll_interval,
    previous_trading_day,
    session_fraction,
)

# A plain Friday, no holiday anywhere near it.
TRADING_DAY = date(2026, 9, 4)


def at(hour: int, minute: int, second: int = 0, day: date = TRADING_DAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, second)


# --- Session state ---------------------------------------------------------


def test_pre_open_ends_the_instant_the_session_opens():
    """The auction and the session are different things and must not overlap."""
    assert market_state(at(9, 14, 59)) is MarketState.PRE_OPEN
    assert market_state(at(9, 15, 0)) is MarketState.OPEN


def test_session_closes_at_1530_not_after():
    assert market_state(at(15, 29, 59)) is MarketState.OPEN
    assert market_state(at(15, 30, 1)) is MarketState.POST_CLOSE


def test_outside_the_session_is_closed():
    assert market_state(at(8, 59, 59)) is MarketState.CLOSED
    assert market_state(at(16, 0, 0)) is MarketState.CLOSED


def test_weekends_and_holidays_are_not_trading_days():
    saturday = date(2026, 9, 5)
    sunday = date(2026, 9, 6)
    republic_day = date(2026, 1, 26)  # a Monday, so not merely a weekend

    for day in (saturday, sunday, republic_day):
        assert not is_trading_day(day)
        # Mid-session on a non-trading day is still HOLIDAY, not OPEN.
        assert market_state(at(11, 0, 0, day=day)) is MarketState.HOLIDAY

    assert is_trading_day(TRADING_DAY)


def test_utc_input_is_converted_rather_than_read_as_ist():
    """04:00 UTC is 09:30 IST, which is inside the session, not before it."""
    from datetime import timezone

    utc_morning = datetime(2026, 9, 4, 4, 0, tzinfo=timezone.utc)
    assert market_state(utc_morning) is MarketState.OPEN


# --- previous_trading_day --------------------------------------------------


def test_previous_trading_day_skips_a_weekend():
    monday = date(2026, 9, 7)
    assert previous_trading_day(monday) == date(2026, 9, 4)  # the Friday


def test_previous_trading_day_skips_a_holiday_stacked_on_a_weekend():
    """Republic Day 2026 is a Monday, so Tuesday's previous close is Friday's.

    This is the case that breaks naive implementations: subtracting one day
    lands on a holiday, subtracting three lands on a Saturday, and only
    walking back until a real trading day is found is correct.
    """
    tuesday = date(2026, 1, 27)
    assert previous_trading_day(tuesday) == date(2026, 1, 23)  # the Friday before


def test_previous_trading_day_is_strictly_before_its_argument():
    assert previous_trading_day(TRADING_DAY) < TRADING_DAY


# --- Volume profile --------------------------------------------------------


def test_session_fraction_is_zero_before_open_and_one_after_close():
    assert session_fraction(at(9, 0)) == 0.0
    assert session_fraction(at(9, 15)) == 0.0
    assert session_fraction(at(15, 30)) == 1.0
    assert session_fraction(at(16, 30)) == 1.0


def test_session_fraction_never_goes_backwards():
    """Cumulative volume cannot decrease, so neither can its expected share."""
    previous = -1.0
    moment = at(9, 0)
    end = at(16, 0)
    while moment <= end:
        current = session_fraction(moment)
        assert current >= previous, f"fraction fell at {moment.time()}"
        previous = current
        moment += timedelta(minutes=1)


def test_volume_profile_is_u_shaped():
    """Midday must be visibly thinner than the open and the close.

    If this is ever flattened into a straight line, relative_volume() starts
    understating the morning and overstating lunchtime, and the volume
    confirmation on every abnormal_move event silently degrades.
    """
    first_half_hour = session_fraction(at(9, 45)) - session_fraction(at(9, 15))
    midday_half_hour = session_fraction(at(12, 30)) - session_fraction(at(12, 0))
    last_half_hour = session_fraction(at(15, 30)) - session_fraction(at(15, 0))

    assert first_half_hour > midday_half_hour
    assert last_half_hour > midday_half_hour


def test_session_fraction_interpolates_between_buckets():
    """Halfway between two buckets is halfway between their fractions."""
    # 09:30 is 0.11 and 10:00 is 0.26, so 09:45 should be 0.185.
    assert abs(session_fraction(at(9, 45)) - 0.185) < 1e-9


# --- Poll cadence ----------------------------------------------------------


def test_poll_interval_backs_off_outside_the_session():
    assert poll_interval(MarketState.OPEN) == settings.poll_interval_open
    assert poll_interval(MarketState.PRE_OPEN) == settings.poll_interval_preopen

    for state in (MarketState.CLOSED, MarketState.POST_CLOSE, MarketState.HOLIDAY):
        assert poll_interval(state) == settings.poll_interval_closed

    assert poll_interval(MarketState.OPEN) < poll_interval(MarketState.CLOSED)
