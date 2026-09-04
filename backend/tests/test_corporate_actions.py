"""Corporate action handling.

The test that matters most in this suite is the first one: it proves the system
does not tell a user their holding collapsed 80% when a stock split.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from app.detect.signals import z_score
from app.statistics.compute import (
    CorporateActionRecord,
    adjust_for_corporate_actions,
    compute_beta,
    compute_symbol_statistics,
)


def make_bars(prices: list[float], volumes: list[float] | None = None) -> pd.DataFrame:
    start = date(2026, 6, 1)
    volumes = volumes or [1_000_000.0] * len(prices)
    return pd.DataFrame(
        {
            "bar_date": [start + timedelta(days=i) for i in range(len(prices))],
            "open": prices,
            "high": [p * 1.01 for p in prices],
            "low": [p * 0.99 for p in prices],
            "close": prices,
            "volume": volumes,
        }
    )


def test_split_does_not_look_like_a_crash():
    """The alert that would destroy the product's credibility."""
    # Trades near 2000, then a 1:5 split takes it to 400.
    prices = [2000.0] * 40 + [400.0] * 5
    split_date = date(2026, 6, 1) + timedelta(days=40)
    bars = make_bars(prices)

    # Without adjustment, the day of the split is a catastrophic return.
    raw_returns = bars["close"].pct_change().dropna()
    assert raw_returns.min() < -0.75

    adjusted = adjust_for_corporate_actions(
        bars,
        [CorporateActionRecord(ex_date=split_date, action_type="split", ratio=5.0)],
    )
    adj_returns = adjusted["adj_close"].pct_change().dropna()

    # After adjustment the series is continuous across the event.
    assert abs(adj_returns.min()) < 1e-9
    assert abs(adj_returns.max()) < 1e-9


def test_split_adjusts_volume_in_the_opposite_direction():
    """Skipping this would inflate RVOL for weeks after any split."""
    prices = [2000.0] * 30 + [400.0] * 5
    split_date = date(2026, 6, 1) + timedelta(days=30)
    bars = make_bars(prices)

    adjusted = adjust_for_corporate_actions(
        bars,
        [CorporateActionRecord(ex_date=split_date, action_type="split", ratio=5.0)],
    )

    pre = adjusted.loc[adjusted["bar_date"] < split_date, "adj_volume"].iloc[-1]
    post = adjusted.loc[adjusted["bar_date"] >= split_date, "adj_volume"].iloc[0]
    assert abs(pre - post * 5.0) < 1e-6


def test_unadjusted_split_would_produce_an_enormous_false_signal():
    """Demonstrates the failure end to end, through the actual scorer."""
    calm_prices = [2000.0 + (i % 3) for i in range(60)]
    bars = make_bars(calm_prices)
    stats = compute_symbol_statistics(bars)

    split_day_return = (400.0 - 2000.0) / 2000.0
    z = z_score(split_day_return, stats)

    # This is the alert the user would have received. It is off the scale.
    assert abs(z) > 100


def test_dividend_does_not_register_as_a_signal():
    prices = [500.0] * 20 + [490.0] * 5
    ex_date = date(2026, 6, 1) + timedelta(days=20)
    bars = make_bars(prices)

    adjusted = adjust_for_corporate_actions(
        bars,
        [CorporateActionRecord(ex_date=ex_date, action_type="dividend", amount=10.0)],
    )
    returns = adjusted["adj_close"].pct_change().dropna()
    assert abs(returns.min()) < 0.005


def test_beta_falls_back_to_one_on_thin_data():
    """A beta from eight observations is noise wearing a number's clothes."""
    short = pd.Series([0.01, -0.02, 0.005, 0.01, -0.01, 0.0, 0.02, -0.005])
    beta, n = compute_beta(short, short)
    assert beta == 1.0
    assert n == 8


def test_beta_is_computed_on_aligned_dates_only():
    """A suspended symbol must not have its Tuesday paired with Friday."""
    dates = pd.date_range("2026-01-01", periods=60, freq="D")
    index_returns = pd.Series([0.01, -0.01] * 30, index=dates)
    # Stock moves at exactly twice the index, but is missing three sessions.
    stock_returns = (index_returns * 2.0).drop(dates[[5, 12, 20]])

    beta, n = compute_beta(stock_returns, index_returns)

    assert n == 57
    assert abs(beta - 2.0) < 1e-6


def test_flat_benchmark_does_not_divide_by_zero():
    dates = pd.date_range("2026-01-01", periods=40, freq="D")
    flat = pd.Series([0.0] * 40, index=dates)
    moving = pd.Series([0.01, -0.01] * 20, index=dates)
    beta, _ = compute_beta(moving, flat)
    assert beta == 1.0


def test_split_restatement_covers_every_stored_level():
    """Regression: adjusting previous_close alone left a false range break.

    Found by the scenario runner. A corporate action invalidates every cached
    price level simultaneously, so ratios must be preserved and levels rescaled.
    """
    from app.detect.signals import SymbolStatistics
    from app.statistics.compute import restate_statistics

    before = SymbolStatistics(
        mean_ret_30d=0.0003, std_ret_30d=0.012, avg_vol_20d=2_000_000,
        high_52w=4200.0, low_52w=3100.0, beta_60d=0.80,
    )
    after = restate_statistics(before, ratio=5.0)

    # Levels are restated into post-split shares.
    assert after.high_52w == 840.0
    assert after.low_52w == 620.0
    # Share counts move the other way.
    assert after.avg_vol_20d == 10_000_000
    # Scale-invariant quantities must be untouched.
    assert after.std_ret_30d == before.std_ret_30d
    assert after.beta_60d == before.beta_60d
    assert after.mean_ret_30d == before.mean_ret_30d


def test_split_day_emits_no_price_signal_after_full_adjustment():
    from datetime import date as _date, datetime as _dt

    from app.detect.detector import TickContext, detect
    from app.detect.signals import SymbolStatistics
    from app.statistics.compute import restate_statistics

    stats = SymbolStatistics(0.0003, 0.012, 2_000_000, 4200.0, 3100.0, 0.80)
    adjusted = restate_statistics(stats, ratio=5.0)

    events = detect(
        TickContext(
            symbol="TCS", price=780.0, previous_close=780.0, open_price=780.0,
            volume_so_far=adjusted.avg_vol_20d * 0.5, session_fraction=0.5,
            index_return=0.0, observed_at=_dt(2026, 9, 4, 10, 0),
            session_date=_date(2026, 9, 4),
        ),
        adjusted,
    )
    assert events == []
