"""Nightly statistics.

Runs once after the close, per symbol. Everything expensive lives here so the
live path stays a subtraction and a division.

Order matters and is not negotiable: corporate actions are applied *before* any
return is computed. Get this backwards and every statistic downstream is built
on a fabricated 80% crash.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from app.detect.signals import SymbolStatistics

# Below this many observations, a beta estimate is noise wearing a number's
# clothes. We fall back to 1.0, which at least encodes the honest prior that a
# stock moves with the market.
MIN_BETA_SAMPLES = 30


@dataclass
class CorporateActionRecord:
    ex_date: date
    action_type: str  # split | bonus | dividend
    ratio: float = 1.0
    amount: float = 0.0


def adjust_for_corporate_actions(
    bars: pd.DataFrame, actions: list[CorporateActionRecord]
) -> pd.DataFrame:
    """Back-adjust the price series so returns reflect economics, not mechanics.

    A 1:5 split takes a 2000 rupee share to 400 overnight. Nothing happened to
    the value of anyone's holding. But a naive close-to-close return reads
    -80%, which would be by far the largest signal the system has ever seen,
    and it would be reported to the user as the most urgent event in their
    watchlist.

    That single false alert is unrecoverable. A user who is told their holding
    collapsed, panics, and then discovers it was a stock split will not trust
    the product again, and they would be right not to.

    Convention: every bar *before* an ex-date is divided by the split ratio, so
    the series is expressed in today's share terms and is continuous across the
    event. The raw close column is left untouched so the adjustment stays
    auditable.

    Expects bars sorted oldest first with a 'bar_date' column.
    """
    if bars.empty:
        return bars

    out = bars.copy()
    if "adj_close" not in out.columns:
        out["adj_close"] = out["close"]
    out["adj_close"] = out["close"].astype(float)
    out["adj_volume"] = out["volume"].astype(float)

    for action in sorted(actions, key=lambda a: a.ex_date):
        prior = out["bar_date"] < action.ex_date

        if action.action_type in ("split", "bonus") and action.ratio > 0:
            # Prices before the event are restated into post-event shares.
            out.loc[prior, "adj_close"] = out.loc[prior, "adj_close"] / action.ratio
            # Volume moves the other way: the same economic quantity is now
            # more shares. Skipping this is a subtle bug that would make every
            # pre-split day look like a volume drought and inflate RVOL for
            # weeks afterwards.
            out.loc[prior, "adj_volume"] = out.loc[prior, "adj_volume"] * action.ratio

        elif action.action_type == "dividend" and action.amount > 0:
            # A stock going ex-dividend drops by roughly the dividend. That is
            # mechanical, not a market judgement, so it should not register as
            # a signal.
            ref = out.loc[prior, "adj_close"]
            if not ref.empty and ref.iloc[-1] > 0:
                factor = 1 - (action.amount / ref.iloc[-1])
                if 0 < factor <= 1:
                    out.loc[prior, "adj_close"] = ref * factor

    return out


def restate_statistics(stats: SymbolStatistics, ratio: float) -> SymbolStatistics:
    """Restate every stored *price level* across a split or bonus.

    Found by the scenario runner rather than by reasoning, which is the whole
    argument for having one. Adjusting previous_close alone still produced a
    false "new 52-week low" on split day, because the stored 52-week range was
    left in pre-split share terms.

    The general lesson, and the thing worth saying out loud: a corporate action
    invalidates every cached price level simultaneously, not just the one you
    happened to be thinking about. Ratios (mean return, standard deviation,
    beta) are scale-invariant and must be left alone; levels are not.
    """
    if ratio <= 0 or ratio == 1.0:
        return stats
    return SymbolStatistics(
        mean_ret_30d=stats.mean_ret_30d,  # a ratio, unaffected by the split
        std_ret_30d=stats.std_ret_30d,  # likewise
        avg_vol_20d=stats.avg_vol_20d * ratio,  # shares, so it scales up
        high_52w=stats.high_52w / ratio,  # a level, so it scales down
        low_52w=stats.low_52w / ratio,
        beta_60d=stats.beta_60d,  # a ratio
    )


def compute_beta(
    stock_returns: pd.Series, index_returns: pd.Series
) -> tuple[float, int]:
    """Sensitivity of this symbol to its benchmark, over aligned dates.

    Aligning on the index is essential: a symbol suspended for three sessions
    has fewer observations than the index, and a positional zip would silently
    pair Tuesday's stock return with Friday's index return, producing a beta
    that is confidently wrong.
    """
    aligned = pd.concat(
        [stock_returns.rename("stock"), index_returns.rename("index")],
        axis=1,
        join="inner",
    ).dropna()

    n = len(aligned)
    if n < MIN_BETA_SAMPLES:
        return 1.0, n

    index_variance = float(aligned["index"].var(ddof=1))
    if index_variance <= 1e-12:
        # A flat benchmark carries no information about sensitivity to it.
        return 1.0, n

    covariance = float(aligned["stock"].cov(aligned["index"]))
    beta = covariance / index_variance

    # Real single-stock betas essentially never leave this range. A value
    # outside it means the input is broken, not that the stock is remarkable.
    return float(np.clip(beta, -1.0, 3.0)), n


def compute_symbol_statistics(
    bars: pd.DataFrame,
    index_bars: pd.DataFrame | None = None,
    actions: list[CorporateActionRecord] | None = None,
) -> SymbolStatistics:
    """Full nightly computation for one symbol.

    bars: at least 60 rows of daily OHLCV, oldest first, columns
          bar_date, open, high, low, close, volume.
    """
    if bars.empty or len(bars) < 2:
        return SymbolStatistics(0.0, 0.0, 0.0, 0.0, 0.0, 1.0)

    adjusted = adjust_for_corporate_actions(bars, actions or [])
    returns = adjusted["adj_close"].pct_change().dropna()

    window = returns.tail(30)
    # ddof=1 because this is a sample estimate of the return distribution, not
    # the population. With 30 observations the difference is about 1.7%, which
    # is small but sits directly under every threshold decision in the system.
    std_30d = float(window.std(ddof=1)) if len(window) > 1 else 0.0

    beta, samples = 1.0, 0
    if index_bars is not None and not index_bars.empty:
        stock_series = returns.copy()
        stock_series.index = adjusted["bar_date"].iloc[1:].values
        index_returns = index_bars["close"].pct_change().dropna()
        index_returns.index = index_bars["bar_date"].iloc[1:].values
        beta, samples = compute_beta(stock_series.tail(60), index_returns.tail(60))

    return SymbolStatistics(
        mean_ret_30d=float(window.mean()) if len(window) else 0.0,
        std_ret_30d=std_30d,
        avg_vol_20d=float(adjusted["adj_volume"].tail(20).mean()),
        high_52w=float(adjusted["adj_close"].tail(252).max()),
        low_52w=float(adjusted["adj_close"].tail(252).min()),
        beta_60d=beta,
    )
