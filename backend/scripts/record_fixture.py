"""Write data/session.jsonl for the replay provider.

    python scripts/record_fixture.py                  # synthesise (offline)
    python scripts/record_fixture.py --mode record    # capture a live session

The synthesised session is built from the betas actually stored in
symbol_stats, not from the betas seed.py intended. Those differ -- a beta is
an estimate from 60 observations and carries real error -- and a fixture built
on the intended number can produce a selloff where the residual quietly falls
below threshold and the correlation collapse, the best thing this product
does, silently fails to fire. Reading the stored value makes the demo robust
against however the estimate landed.

Scenarios in the session, in order:

    09:15-11:30  quiet drift, +/-0.3%
    11:30-11:45  market-wide selloff, index -2.2%, every symbol falling by
                 its own beta -- except IRFC, which holds flat
    11:45-14:00  the new level holds
    all session  TCS trades ex a 1:5 split: quoted 80% below a
                 previous_close the feed has not restated, with a corporate
                 action for today planted so the worker can explain it
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.market import calendar  # noqa: E402
from app.market.calendar import MarketState  # noqa: E402
from app.models import CorporateAction, DailyBar, SymbolStats  # noqa: E402
from app.providers.factory import provider  # noqa: E402

log = logging.getLogger("fixture")

SEED = 7
STEP = timedelta(seconds=30)
OPEN = time(9, 15)
CLOSE = time(15, 30)

SELLOFF_START = time(11, 30)
SELLOFF_END = time(11, 45)
SELLOFF_INDEX_RETURN = -0.022

SPLIT_SYMBOL = "TCS"
SPLIT_RATIO = 5.0

# The one symbol that does not follow the market down. Its beta says it should
# have fallen hardest, which is exactly what makes holding flat informative.
HOLDOUT = "IRFC"
INDEX_SYMBOL = "^NSEI"


def _session_day() -> date:
    today = date.today()
    return today if calendar.is_trading_day(today) else calendar.previous_trading_day(today)


def _load_universe(session) -> dict[str, dict]:
    """Last close and stored beta per symbol."""
    last_bars = {}
    for symbol, bar_date, close in session.query(
        DailyBar.symbol, DailyBar.bar_date, DailyBar.close
    ).order_by(DailyBar.symbol, DailyBar.bar_date):
        last_bars[symbol] = close

    universe = {}
    for symbol, close in last_bars.items():
        stats = session.get(SymbolStats, symbol)
        universe[symbol] = {
            "previous_close": float(close),
            "beta": float(stats.beta_60d) if stats else 1.0,
            "avg_volume": float(stats.avg_vol_20d) if stats else 1_000_000.0,
        }
    return universe


def _index_path(stamps: list[datetime], rng: np.random.Generator) -> dict[datetime, float]:
    """Cumulative index return at each moment of the session."""
    path: dict[datetime, float] = {}
    drift = 0.0
    for stamp in stamps:
        moment = stamp.time()
        if SELLOFF_START <= moment < SELLOFF_END:
            # Linear slide through the selloff window.
            span = (
                datetime.combine(stamp.date(), SELLOFF_END)
                - datetime.combine(stamp.date(), SELLOFF_START)
            ).total_seconds()
            done = (
                stamp - datetime.combine(stamp.date(), SELLOFF_START)
            ).total_seconds()
            drift = SELLOFF_INDEX_RETURN * (done / span)
        elif moment >= SELLOFF_END:
            drift = SELLOFF_INDEX_RETURN + float(rng.normal(0, 0.0004))
        else:
            drift += float(rng.normal(0, 0.0003))
            drift = max(min(drift, 0.003), -0.003)
        path[stamp] = drift
    return path


def synthesise(out_path: Path) -> int:
    rng = np.random.default_rng(SEED)
    day = _session_day()

    with SessionLocal() as session:
        universe = _load_universe(session)
        if not universe:
            raise SystemExit("no bars found - run scripts/seed.py first")

        # Deliberately read, never written. The action has to exist before
        # the nightly run that seed.py performs, or the stored 52-week range
        # stays in old shares and the split reports as a new 52-week low.
        # Planting it from here would be too late by construction.
        action = (
            session.query(CorporateAction)
            .filter(
                CorporateAction.symbol == SPLIT_SYMBOL,
                CorporateAction.ex_date == day,
                CorporateAction.action_type == "split",
            )
            .one_or_none()
        )
        if action is None:
            raise SystemExit(
                f"no {SPLIT_SYMBOL} split dated {day} - run scripts/seed.py first"
            )

    stamps: list[datetime] = []
    cursor = datetime.combine(day, OPEN)
    end = datetime.combine(day, CLOSE)
    while cursor <= end:
        stamps.append(cursor)
        cursor += STEP

    index_returns = _index_path(stamps, rng)
    opens = {
        symbol: round(info["previous_close"] * (1 + float(rng.normal(0, 0.0008))), 2)
        for symbol, info in universe.items()
    }
    # The opening print on an ex-date is in new shares like every other print
    # that session. Leaving it in old shares makes overnight_gap() compare a
    # pre-split open against a restated previous_close and report a 399% gap.
    opens[SPLIT_SYMBOL] = round(opens[SPLIT_SYMBOL] / SPLIT_RATIO, 2)

    written = 0
    with out_path.open("w", encoding="utf-8") as handle:
        # Written in time order across all symbols: ReplayProvider scans
        # frames sequentially and stops at the first one past the virtual
        # clock, so an out-of-order file silently returns stale prices.
        for stamp in stamps:
            index_return = index_returns[stamp]
            fraction = max(calendar.session_fraction(stamp), 0.01)

            for symbol, info in universe.items():
                if symbol == INDEX_SYMBOL:
                    move = index_return
                elif symbol == HOLDOUT:
                    # Flat through the selloff. Its beta predicted the largest
                    # fall on the list, so standing still is the story.
                    move = float(rng.normal(0.0002, 0.0006))
                else:
                    move = info["beta"] * index_return + float(rng.normal(0, 0.0006))

                price = info["previous_close"] * (1 + move)
                if symbol == SPLIT_SYMBOL:
                    # Quoted in new shares from the opening bell, because that
                    # is when an ex-date takes effect. The feed does not adjust
                    # previous_close to match, which is exactly the situation
                    # the worker has to survive: an 80% apparent overnight drop
                    # that means nothing.
                    #
                    # Deliberately NOT dropped mid-session. A 5x fall between
                    # two consecutive ticks is not a split, it is a bad print,
                    # and validate_tick correctly quarantines it -- so the
                    # post-split price never reaches the cache while
                    # previous_close has already been restated, and the
                    # detector reports a 400% rally and a false 52-week high.
                    # Measured, not theorised.
                    price /= SPLIT_RATIO

                handle.write(
                    json.dumps(
                        {
                            "symbol": symbol,
                            "as_of": stamp.isoformat(),
                            "price": round(price, 2),
                            "open": opens[symbol],
                            "previous_close": round(info["previous_close"], 2),
                            "volume": round(info["avg_volume"] * fraction, 0),
                        }
                    )
                    + "\n"
                )
                written += 1

    log.info("wrote %d frames for %d symbols to %s", written, len(universe), out_path)
    log.info("session %s %s-%s at %s intervals", day, OPEN, CLOSE, STEP)
    return written


def record(out_path: Path) -> int:
    """Capture a live session from the configured provider."""
    now = provider.now()
    if calendar.market_state(now) is not MarketState.OPEN:
        raise SystemExit(
            f"market is {calendar.market_state(now).value}; nothing to record. "
            "Use --mode synthesise."
        )

    with SessionLocal() as session:
        symbols = [row[0] for row in session.query(DailyBar.symbol).distinct()]

    quotes = provider.fetch(symbols)
    if not quotes:
        raise SystemExit("provider returned nothing; see the README known issues")

    with out_path.open("a", encoding="utf-8") as handle:
        for symbol, quote in sorted(quotes.items()):
            handle.write(
                json.dumps(
                    {
                        "symbol": symbol,
                        "as_of": quote.as_of.isoformat(),
                        "price": quote.price,
                        "open": quote.open,
                        "previous_close": quote.previous_close,
                        "volume": quote.volume,
                    }
                )
                + "\n"
            )
    log.info("appended %d live frames to %s", len(quotes), out_path)
    return len(quotes)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("synthesise", "record"), default="synthesise")
    parser.add_argument("--out", default=settings.replay_fixture)
    args = parser.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if args.mode == "record":
        record(out_path)
    else:
        synthesise(out_path)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    main()
