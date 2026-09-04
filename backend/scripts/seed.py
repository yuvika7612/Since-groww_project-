"""Seed enough data for the demo to work with no market and no network.

    python scripts/seed.py

Deterministic: the same seed produces the same prices every run, so a demo
rehearsed on Tuesday behaves identically on Thursday.

The important choice here is *how* the synthetic returns are generated. The
obvious approach -- an independent random walk per symbol -- produces symbols
with no relationship to the index, so every beta computes to roughly zero and
every residual collapses to the raw move. The correlation-collapse scenario,
which is the best thing this product does, then cannot fire at all.

So each symbol is generated as `beta * index_return + idiosyncratic noise`,
with the noise scaled to hit that symbol's target volatility. The betas the
nightly job recovers from these bars are then close to the ones intended, and
"IRFC held flat when its beta predicted a 3.4% fall" is a true statement about
the data rather than a caption.
"""

from __future__ import annotations

import logging
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal, init_db  # noqa: E402
from app.market import calendar  # noqa: E402
from app.models import (  # noqa: E402
    CorporateAction,
    DailyBar,
    Symbol,
    User,
    Watchlist,
    WatchlistItem,
)
from workers import nightly  # noqa: E402

log = logging.getLogger("seed")

SEED = 42
DAYS = 90
INDEX_SYMBOL = "^NSEI"
INDEX_VOL = 0.011
INDEX_DRIFT = 0.0004
DEMO_EMAIL = "demo@since.app"

# symbol -> (name, sector, start price, daily volatility, beta, avg daily volume)
#
# The volatilities are deliberately more compressed than real NSE ones, and
# the reason is worth stating because it is a real constraint rather than a
# fudge. compute_beta() regresses 60 daily returns, so the standard error of
# the estimate is roughly sigma_residual / (sigma_index * sqrt(60)). Give IRFC
# a realistic 6% daily volatility against a 0.8% index and that error is 0.95:
# a beta of 1.6 and a beta of 0.7 become statistically indistinguishable, the
# stored beta is noise, and the residual it feeds is meaningless.
#
# Real data has this problem too -- it is why sample_size is stored at all --
# but a fixture whose headline number is dominated by estimation error cannot
# demonstrate anything. So the index is given a slightly livelier 1.1% and the
# two volatile names are damped to where 60 observations can actually resolve
# their beta. IRFC is still comfortably the most volatile symbol on the list.
SYMBOLS: dict[str, tuple[str, str, float, float, float, float]] = {
    "RELIANCE":   ("Reliance Industries", "Energy", 2850.0, 0.014, 1.05, 8_000_000),
    "TCS":        ("Tata Consultancy Services", "IT", 3900.0, 0.013, 0.80, 2_000_000),
    "HDFCBANK":   ("HDFC Bank", "Banking", 1650.0, 0.012, 0.95, 4_000_000),
    "INFY":       ("Infosys", "IT", 1720.0, 0.015, 0.90, 5_000_000),
    "ICICIBANK":  ("ICICI Bank", "Banking", 1180.0, 0.014, 1.10, 6_000_000),
    "HINDUNILVR": ("Hindustan Unilever", "FMCG", 2450.0, 0.010, 0.55, 1_500_000),
    "ITC":        ("ITC Limited", "FMCG", 460.0, 0.011, 0.60, 9_000_000),
    "SBIN":       ("State Bank of India", "Banking", 820.0, 0.017, 1.25, 12_000_000),
    "IRFC":       ("Indian Railway Finance Corporation", "Finance", 148.0, 0.026, 1.60, 900_000),
    "ADANIENT":   ("Adani Enterprises", "Conglomerate", 2950.0, 0.022, 1.45, 3_000_000),
}

# Dated well inside the bar series rather than onto the replayed session.
#
# An ex-date on the session itself means every scenario carries a corporate
# action, so the quiet day is never quiet: it correctly but unhelpfully
# reports the split, and the screen that is supposed to say "nothing
# meaningful happened" cannot. Sixty days back exercises the nightly
# back-adjustment (which is where the 52-week-range bug lived) and leaves the
# replayed session clean.
#
# The split *scenario* provisions its own same-day action when it runs; see
# app/api/debug.py:run_scenario.
SPLIT_SYMBOL = "TCS"
SPLIT_RATIO = 5.0
SPLIT_DAYS_AGO = 60


def trading_days(ending: date, count: int) -> list[date]:
    """The last `count` trading days ending on or before `ending`."""
    days: list[date] = []
    cursor = ending
    while len(days) < count:
        if calendar.is_trading_day(cursor):
            days.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(days)


def _returns(rng: np.random.Generator, index_returns: np.ndarray,
             volatility: float, beta: float) -> np.ndarray:
    """beta * market + noise, with the noise sized to hit `volatility`.

    var(r) = beta^2 * var(index) + var(noise), so the residual variance is
    whatever is left over. The floor keeps a low-beta, low-vol symbol from
    asking for negative variance.
    """
    explained = (beta * INDEX_VOL) ** 2
    residual = max(volatility**2 - explained, (0.25 * volatility) ** 2)
    noise = rng.normal(0.0, np.sqrt(residual), len(index_returns))
    return INDEX_DRIFT + beta * index_returns + noise


def _bars_for(symbol: str, prices: np.ndarray, volumes: np.ndarray,
              days: list[date], rng: np.random.Generator) -> list[DailyBar]:
    bars = []
    for i, day in enumerate(days):
        close = float(prices[i])
        # A plausible intraday range around the close. Not used by any signal
        # the product computes, but a bar with high == low == close looks
        # obviously fake to anyone who opens the table.
        spread = close * float(abs(rng.normal(0.004, 0.002)))
        open_price = close * (1 + float(rng.normal(0, 0.003)))
        bars.append(
            DailyBar(
                symbol=symbol,
                bar_date=day,
                open=round(open_price, 2),
                high=round(max(open_price, close) + spread / 2, 2),
                low=round(min(open_price, close) - spread / 2, 2),
                close=round(close, 2),
                adj_close=round(close, 2),
                volume=float(volumes[i]),
            )
        )
    return bars


def run() -> None:
    init_db()
    rng = np.random.default_rng(SEED)

    # Bars end the trading day *before* the replayed session, so the fixture's
    # previous_close is a real close rather than a bar for a day that is
    # simultaneously being replayed tick by tick.
    today = date.today()
    session_day = today if calendar.is_trading_day(today) else calendar.previous_trading_day(today)
    last_bar_day = calendar.previous_trading_day(session_day)
    days = trading_days(last_bar_day, DAYS)
    split_date = today - timedelta(days=SPLIT_DAYS_AGO)

    index_returns = rng.normal(INDEX_DRIFT, INDEX_VOL, DAYS)

    with SessionLocal() as session:
        # --- instruments ---------------------------------------------------
        session.merge(
            Symbol(symbol=INDEX_SYMBOL, name="Nifty 50", exchange="NSE",
                   sector="Index", benchmark=INDEX_SYMBOL)
        )
        for symbol, (name, sector, *_rest) in SYMBOLS.items():
            session.merge(
                Symbol(symbol=symbol, name=name, exchange="NSE", sector=sector,
                       benchmark=INDEX_SYMBOL)
            )
        session.flush()

        # --- bars, rebuilt from scratch so re-running is idempotent --------
        every_symbol = [INDEX_SYMBOL, *SYMBOLS]
        session.query(DailyBar).filter(DailyBar.symbol.in_(every_symbol)).delete(
            synchronize_session=False
        )
        session.query(CorporateAction).filter(
            CorporateAction.symbol.in_(every_symbol)
        ).delete(synchronize_session=False)

        index_prices = 24500.0 * np.exp(np.cumsum(index_returns))
        index_volumes = np.full(DAYS, 0.0)
        session.add_all(_bars_for(INDEX_SYMBOL, index_prices, index_volumes, days, rng))

        for symbol, (_name, _sector, start, vol, beta, avg_volume) in SYMBOLS.items():
            returns = _returns(rng, index_returns, vol, beta)
            prices = start * np.exp(np.cumsum(returns))
            volumes = np.abs(rng.normal(avg_volume, avg_volume * 0.25, DAYS))

            if symbol == SPLIT_SYMBOL:
                # Real pre-split prints are quoted in old shares, so the raw
                # series has to actually contain the 80% drop for the nightly
                # back-adjustment to have something to remove. Seeding a
                # smooth series and then adjusting it would *create* a
                # discontinuity rather than repair one.
                before = np.array([d < split_date for d in days])
                prices = np.where(before, prices * SPLIT_RATIO, prices)
                volumes = np.where(before, volumes / SPLIT_RATIO, volumes)

            session.add_all(_bars_for(symbol, prices, volumes, days, rng))

        session.add(
            CorporateAction(
                symbol=SPLIT_SYMBOL, ex_date=split_date, action_type="split",
                ratio=SPLIT_RATIO, amount=0.0,
            )
        )

        # --- demo user -----------------------------------------------------
        user = session.query(User).filter(User.email == DEMO_EMAIL).one_or_none()
        if user is None:
            user = User(email=DEMO_EMAIL)
            session.add(user)
            session.flush()

        watchlist = (
            session.query(Watchlist).filter(Watchlist.user_id == user.id).first()
        )
        if watchlist is None:
            watchlist = Watchlist(user_id=user.id, name="My watchlist")
            session.add(watchlist)
            session.flush()

        existing_items = {
            item.symbol
            for item in session.query(WatchlistItem).filter(
                WatchlistItem.watchlist_id == watchlist.id
            )
        }
        for symbol in SYMBOLS:
            if symbol not in existing_items:
                session.add(WatchlistItem(watchlist_id=watchlist.id, symbol=symbol))

        session.commit()

        log.info("symbols: %d + index", len(SYMBOLS))
        log.info("bars:    %d trading days, %s to %s", DAYS, days[0], days[-1])
        log.info("split:   %s 1:%g ex-date %s", SPLIT_SYMBOL, SPLIT_RATIO, split_date)
        log.info("user:    %s (id %d), watchlist %d with %d symbols",
                 DEMO_EMAIL, user.id, watchlist.id, len(SYMBOLS))

    log.info("computing statistics...")
    result = nightly.run()
    log.info("seeded. %d symbols have statistics.", result["computed"])
    log.info("next: python scripts/record_fixture.py, then uvicorn app.main:app")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    run()
