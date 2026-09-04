"""Nightly statistics recompute.

    python -m workers.nightly

Everything expensive lives here so the live path stays a subtraction and a
division. Runs after the close, or on demand.

Order is not negotiable: corporate actions are applied before any return is
computed. Get it backwards and every statistic downstream is built on a
fabricated 80% crash.

Bars are read from `daily_bars` rather than fetched here. Ingesting end-of-day
bars and computing statistics from them are separate jobs with separate
failure modes, and keeping them apart means a broken upstream delays fresh
bars without also destroying the statistics computed from the ones we have.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

import pandas as pd
from sqlalchemy import select

from app.cache import cache
from app.db import SessionLocal
from app.market.calendar import IST
from app.models import CorporateAction, DailyBar, SymbolStats
from app.statistics.compute import (
    MIN_BETA_SAMPLES,
    CorporateActionRecord,
    compute_symbol_statistics,
)

log = logging.getLogger(__name__)

INDEX_SYMBOL = "^NSEI"


def _now_ist() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def _target_symbols(session) -> list[str]:
    """Everything tracked or currently watched.

    The union matters in both directions: a symbol in the hot set nobody has
    computed statistics for yet needs its first run, and a symbol that has
    statistics but has dropped out of every watchlist still needs them kept
    current in case somebody adds it back tomorrow.
    """
    tracked = set(session.scalars(select(SymbolStats.symbol)))
    active = set(cache.hot_set())
    bars_present = set(session.scalars(select(DailyBar.symbol).distinct()))
    return sorted((tracked | active | bars_present) - {INDEX_SYMBOL})


def _load_bars(session, symbols: list[str]) -> dict[str, pd.DataFrame]:
    """Every symbol's bars in one query, grouped in memory."""
    rows = session.execute(
        select(
            DailyBar.symbol, DailyBar.bar_date, DailyBar.open, DailyBar.high,
            DailyBar.low, DailyBar.close, DailyBar.volume,
        )
        .where(DailyBar.symbol.in_(symbols + [INDEX_SYMBOL]))
        .order_by(DailyBar.symbol, DailyBar.bar_date)
    ).all()

    if not rows:
        return {}

    frame = pd.DataFrame(
        rows, columns=["symbol", "bar_date", "open", "high", "low", "close", "volume"]
    )
    return {
        symbol: group.drop(columns="symbol").reset_index(drop=True)
        for symbol, group in frame.groupby("symbol")
    }


def _load_actions(session, symbols: list[str]) -> dict[str, list[CorporateAction]]:
    grouped: dict[str, list[CorporateAction]] = {}
    rows = session.scalars(
        select(CorporateAction)
        .where(CorporateAction.symbol.in_(symbols))
        .order_by(CorporateAction.ex_date)
    ).all()
    for row in rows:
        grouped.setdefault(row.symbol, []).append(row)
    return grouped


# WHY restate_statistics() IS NOT CALLED HERE, despite being the fix for the
# bug the scenario runner caught.
#
# compute_symbol_statistics() already calls adjust_for_corporate_actions(),
# which back-adjusts every bar dated before an ex-date. That covers both cases
# a nightly run can see:
#
#   action inside the bar range  -> the bars before it are divided, the series
#                                   is continuous, levels come out correct
#   action after the last bar    -> `prior` matches every row, so the whole
#                                   series is restated forward, which is also
#                                   correct
#
# So the levels this job computes are already in post-action share terms, and
# calling restate_statistics on top divides them by the ratio a second time.
# Measured on the seeded TCS 1:5: correct is high_52w 849, double-adjusted
# gives 171.
#
# restate_statistics earns its place in the *live* path instead --
# workers/ingest.py:_apply_corporate_action -- where last night's statistics
# were computed in pre-split terms and a split lands during today's session.
# There is nothing to recompute from intraday, so the stored levels have to be
# restated in place. That is the case it was written for.


def run() -> dict:
    started = time.monotonic()
    today = _now_ist().date()
    computed = thin = skipped = 0

    with SessionLocal() as session:
        symbols = _target_symbols(session)
        if not symbols:
            log.warning("no symbols to compute; run scripts/seed.py first")
            return {"computed": 0, "skipped": 0, "thin": 0}

        bars_by_symbol = _load_bars(session, symbols)
        actions_by_symbol = _load_actions(session, symbols)
        index_bars = bars_by_symbol.get(INDEX_SYMBOL)
        existing = {
            row.symbol: row
            for row in session.scalars(
                select(SymbolStats).where(SymbolStats.symbol.in_(symbols))
            )
        }

        for symbol in symbols:
            bars = bars_by_symbol.get(symbol)
            if bars is None or len(bars) < 2:
                log.warning("%s: no usable bars, skipping", symbol)
                skipped += 1
                continue

            actions = actions_by_symbol.get(symbol, [])
            records = [
                CorporateActionRecord(
                    ex_date=a.ex_date,
                    action_type=a.action_type,
                    ratio=a.ratio,
                    amount=a.amount,
                )
                for a in actions
            ]

            # Corporate actions are applied inside this call, before any
            # return is computed. See the note above on why no further
            # restatement follows it.
            stats = compute_symbol_statistics(bars, index_bars, records)

            # Sample size drives whether beta is trustworthy at all, so it is
            # recorded rather than inferred.
            sample_size = max(len(bars) - 1, 0)
            if sample_size < MIN_BETA_SAMPLES:
                log.warning(
                    "%s: only %d observations, beta fell back to 1.0",
                    symbol, sample_size,
                )
                thin += 1

            row = existing.get(symbol)
            if row is None:
                row = SymbolStats(symbol=symbol)
                session.add(row)
            # Upsert, never insert: running this twice must produce identical
            # rows rather than a second generation of them.
            row.mean_ret_30d = stats.mean_ret_30d
            row.std_ret_30d = stats.std_ret_30d
            row.avg_vol_20d = stats.avg_vol_20d
            row.high_52w = stats.high_52w
            row.low_52w = stats.low_52w
            row.beta_60d = stats.beta_60d
            row.sample_size = sample_size
            row.computed_at = _now_ist()
            computed += 1

        session.commit()

    duration = time.monotonic() - started
    log.info(
        "nightly: %d computed, %d skipped, %d with thin history, %.2fs",
        computed, skipped, thin, duration,
    )
    return {"computed": computed, "skipped": skipped, "thin": thin}


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    run()
