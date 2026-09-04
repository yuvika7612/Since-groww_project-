"""Tests for digest assembly.

These encode the product claims. If any of them fails, the pitch is no longer
true.
"""

from __future__ import annotations

from datetime import date, datetime

from app.detect.events import ChangeEvent, EventType
from app.digest.service import (
    DigestRow,
    MarketContext,
    apply_attention_budget,
    build_digest,
    collapse_correlated,
    compute_market_context,
)
from app.providers.base import Freshness

NOW = datetime(2026, 9, 4, 15, 30)
TODAY = date(2026, 9, 4)


def event(symbol: str, etype: EventType, severity: float) -> ChangeEvent:
    return ChangeEvent(
        symbol=symbol,
        type=etype,
        severity=severity,
        occurred_at=NOW,
        session_date=TODAY,
        explanation=f"{etype.value} on {symbol}",
    )


def row(symbol: str, change: float, events=None, freshness=Freshness.LIVE, note=None):
    return DigestRow(
        symbol=symbol,
        name=symbol,
        price=100.0,
        freshness=freshness,
        as_of=NOW,
        change_since_seen=change,
        seen_at=NOW,
        events=events or [],
        data_note=note,
    )


def test_market_wide_selloff_collapses_to_the_exceptions():
    """Twelve rows saying the same thing are one piece of information."""
    followers = [
        row(f"S{i}", -0.02, [event(f"S{i}", EventType.ABNORMAL_MOVE, 0.6)])
        for i in range(11)
    ]
    exception = row(
        "HELD",
        0.0,
        [event("HELD", EventType.IDIOSYNCRATIC_MOVE, 0.7)],
    )
    market = MarketContext("^NSEI", index_return=-0.021, breadth=0.9)

    survivors = collapse_correlated(followers + [exception], market)

    assert [r.symbol for r in survivors] == ["HELD"]


def test_no_collapse_when_the_move_is_not_market_wide():
    """Poor breadth means the index move is a few heavyweights, not the market."""
    rows = [row(f"S{i}", -0.02, [event(f"S{i}", EventType.ABNORMAL_MOVE, 0.6)])
            for i in range(4)]
    market = MarketContext("^NSEI", index_return=-0.012, breadth=0.3)

    assert len(collapse_correlated(rows, market)) == 4


def test_attention_budget_caps_output_on_a_violent_day():
    rows = [
        row(f"S{i}", -0.05, [event(f"S{i}", EventType.ABNORMAL_MOVE, 0.9)])
        for i in range(20)
    ]
    surfaced, rest = apply_attention_budget(rows, budget=5)
    assert len(surfaced) == 5
    assert len(rest) == 15


def test_attention_budget_lowers_the_bar_on_a_quiet_day():
    """A mild signal earns a slot when nothing else is competing for it."""
    rows = [
        row("MILD", 0.011, [event("MILD", EventType.IDIOSYNCRATIC_MOVE, 0.22)]),
        row("FLAT", 0.001),
        row("ALSOFLAT", 0.0),
    ]
    surfaced, _ = apply_attention_budget(rows, budget=5)
    assert [r.symbol for r in surfaced] == ["MILD"]


def test_rows_with_no_events_never_surface():
    rows = [row("FLAT", 0.001), row("ALSOFLAT", -0.002)]
    surfaced, rest = apply_attention_budget(rows, budget=5)
    assert surfaced == []
    assert len(rest) == 2


def test_ranking_favours_one_severe_signal_over_several_mild_ones():
    severe = row("SEVERE", -0.06, [event("SEVERE", EventType.ABNORMAL_MOVE, 0.95)])
    noisy = row(
        "NOISY",
        -0.01,
        [
            event("NOISY", EventType.ABNORMAL_MOVE, 0.30),
            event("NOISY", EventType.VOLUME_SPIKE, 0.28),
            event("NOISY", EventType.GAP, 0.25),
        ],
    )
    surfaced, _ = apply_attention_budget([noisy, severe], budget=5)
    assert surfaced[0].symbol == "SEVERE"


def test_stale_rows_are_separated_not_ranked():
    """A broken feed is our failure, and must not compete as market news."""
    stale = row("DEAD", 0.0, freshness=Freshness.STALE)
    live = row("LIVE", -0.03, [event("LIVE", EventType.ABNORMAL_MOVE, 0.8)])

    digest = build_digest([stale, live], market=None, now=NOW)

    assert [r.symbol for r in digest.degraded] == ["DEAD"]
    assert [r.symbol for r in digest.needs_attention] == ["LIVE"]


def test_quiet_symbols_are_counted_and_reported():
    rows = [row(f"S{i}", 0.001) for i in range(9)]
    rows.append(row("MOVER", -0.04, [event("MOVER", EventType.ABNORMAL_MOVE, 0.8)]))

    digest = build_digest(rows, market=None, now=NOW)

    assert len(digest.needs_attention) == 1
    assert len(digest.quiet) == 9
    assert "9 other symbols" in digest.quiet_summary


def test_breadth_measures_how_many_followed_the_index():
    rows = [row(f"DOWN{i}", -0.02) for i in range(8)]
    rows += [row(f"UP{i}", 0.01) for i in range(2)]

    market = compute_market_context("^NSEI", -0.02, rows)

    assert abs(market.breadth - 0.8) < 1e-9
    assert market.is_market_wide
