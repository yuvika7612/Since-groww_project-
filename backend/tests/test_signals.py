"""Tests for the signal layer.

These cover the cases that are easy to get wrong and impossible to eyeball:
degenerate distributions, time-of-day volume, and the market-relative logic.
"""

from __future__ import annotations

from datetime import date, datetime

from app.detect.detector import TickContext, detect
from app.detect.events import EventType
from app.detect.signals import (
    SymbolStatistics,
    relative_volume,
    residual_return,
    z_score,
)

CALM = SymbolStatistics(
    mean_ret_30d=0.0005,
    std_ret_30d=0.010,  # a 1% typical day, like a large-cap bank
    avg_vol_20d=1_000_000,
    high_52w=1100.0,
    low_52w=850.0,
    beta_60d=1.0,
)

WILD = SymbolStatistics(
    mean_ret_30d=0.001,
    std_ret_30d=0.060,  # a 6% typical day, like a smallcap
    avg_vol_20d=200_000,
    high_52w=400.0,
    low_52w=90.0,
    beta_60d=1.4,
)


def test_same_move_scores_differently_by_symbol():
    """The central claim of the product, as a test."""
    move = 0.04
    assert z_score(move, CALM) > 3.5
    assert z_score(move, WILD) < 1.0


def test_degenerate_sigma_returns_zero_not_infinity():
    """A suspended symbol must drop out of ranking, not dominate it."""
    frozen = SymbolStatistics(0.0, 0.0, 1000, 100, 50)
    assert z_score(0.25, frozen) == 0.0
    assert not frozen.is_scorable


def test_rvol_adjusts_for_time_of_day():
    """Early-session volume must not be judged against a full-day average."""
    # 20% of the day's normal volume, 20% of the way through the session.
    on_pace = relative_volume(200_000, CALM, session_fraction=0.20)
    assert abs(on_pace - 1.0) < 1e-9

    # Without the adjustment this would look like 0.2x and read as dead.
    naive = 200_000 / CALM.avg_vol_20d
    assert naive < 0.25


def test_residual_ignores_market_wide_moves():
    """A beta-1 stock matching a market selloff is not news."""
    assert abs(residual_return(-0.02, -0.02, CALM)) < 1e-9


def test_flat_stock_on_a_red_day_is_news():
    """The row every other watchlist renders as unremarkable grey."""
    residual = residual_return(0.0, -0.025, CALM)
    assert residual > 0.02


def _ctx(**kw):
    base = dict(
        symbol="TEST",
        price=1000.0,
        previous_close=1000.0,
        open_price=1000.0,
        volume_so_far=200_000,
        session_fraction=0.2,
        index_return=0.0,
        observed_at=datetime(2026, 9, 4, 10, 30),
        session_date=date(2026, 9, 4),
    )
    base.update(kw)
    return TickContext(**base)


def test_quiet_session_emits_nothing():
    """Silence is the correct output most of the time."""
    assert detect(_ctx(), CALM) == []


def test_market_wide_selloff_is_not_flagged_as_idiosyncratic():
    events = detect(_ctx(price=980.0, index_return=-0.02), CALM)
    types = {e.type for e in events}
    # The 2 sigma move is real and gets reported.
    assert EventType.ABNORMAL_MOVE in types
    # But it is explained entirely by the market, so no stock-specific event.
    assert EventType.IDIOSYNCRATIC_MOVE not in types


def test_stock_holding_up_in_a_selloff_is_flagged():
    events = detect(_ctx(price=1000.0, index_return=-0.025), CALM)
    types = {e.type for e in events}
    assert EventType.IDIOSYNCRATIC_MOVE in types
    # Its own return is zero, so nothing "abnormal" happened to it in
    # isolation. Only the market-relative view sees it at all.
    assert EventType.ABNORMAL_MOVE not in types


def test_events_carry_a_readable_explanation():
    events = detect(_ctx(price=1045.0), CALM)
    assert events
    for event in events:
        assert len(event.explanation) > 20
        assert 0.0 <= event.severity <= 1.0


def test_dedupe_key_is_stable_within_a_severity_bucket():
    a = detect(_ctx(price=1045.0), CALM)[0]
    b = detect(_ctx(price=1046.0), CALM)[0]
    assert a.dedupe_key == b.dedupe_key


def test_range_break_fires_on_new_high():
    events = detect(_ctx(price=1120.0, previous_close=1105.0), CALM)
    assert EventType.RANGE_BREAK in {e.type for e in events}
