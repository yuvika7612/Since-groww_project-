"""Live market data via Yahoo Finance.

Honestly labelled as what it is: a free, delayed, best-effort feed. Every
quote it produces carries `Freshness.DELAYED`, never `LIVE`. Rendering a
delayed feed as live is precisely the dishonesty this system is built to
avoid, and the label is set here at the source so no downstream code has to
remember to apply it.

Partial failure is the normal case, not the exception. One delisted ticker,
one malformed frame, one symbol Yahoo simply has no intraday data for — none
of those may take down the poll cycle for the other two thousand.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime

import pandas as pd
import yfinance as yf

from app.market.calendar import (
    IST,
    MarketState,
    is_trading_day,
    market_state,
    previous_trading_day,
)
from app.providers.base import Freshness, MarketDataProvider, Quote

log = logging.getLogger(__name__)

# Sleeps *between* attempts, so five attempts total before the cycle is
# skipped. Yahoo surfaces rate limiting as a generic exception with a 429 in
# the message, and a transient network failure deserves exactly the same
# backoff, so no attempt is made to tell them apart.
_BACKOFF_SECONDS = (1, 2, 4, 8)

# Deliberately wider than the two days the data actually needs.
#
# `period="2d"` cannot satisfy the previous_close rule. On the Tuesday after a
# Monday holiday, two days of intraday data is Monday (empty, it was a
# holiday) and Tuesday, so the previous trading day -- Friday -- is not in the
# window at all. That is the exact long-weekend case previous_trading_day()
# exists to get right, so the window has to be wide enough to contain it.
_HISTORY_PERIOD = "5d"


class YFinanceProvider(MarketDataProvider):
    name = "yfinance"

    def now(self) -> datetime:
        """Wall clock, expressed in IST.

        Naive datetimes mean IST everywhere in this system. `datetime.now()`
        alone would return whatever timezone the host happens to be in, which
        is UTC in a container, and every session boundary would then be off by
        five and a half hours.
        """
        return datetime.now(IST).replace(tzinfo=None)

    @staticmethod
    def _ticker(symbol: str) -> str:
        """Internal symbol to Yahoo ticker.

        NSE equities take a .NS suffix; indices are already in Yahoo's own
        namespace (^NSEI) and adding the suffix would make them un-resolvable.
        """
        return symbol if symbol.startswith("^") else f"{symbol}.NS"

    def fetch(self, symbols: list[str]) -> dict[str, Quote]:
        if not symbols:
            return {}

        by_ticker = {self._ticker(s): s for s in symbols}
        now = self.now()

        # Outside a live session an empty response is the correct answer, not
        # a failure, so retrying would burn fifteen seconds per cycle all
        # night for nothing.
        in_session = market_state(now) is MarketState.OPEN
        raw = self._download(list(by_ticker), retry_on_empty=in_session)
        if raw is None or raw.empty:
            return {}

        today = now.date()
        session_day = today if is_trading_day(today) else previous_trading_day(today)
        prev_day = previous_trading_day(session_day)

        quotes: dict[str, Quote] = {}
        for ticker, symbol in by_ticker.items():
            try:
                quote = self._build_quote(raw, ticker, symbol, session_day, prev_day)
            except Exception as exc:
                # Per symbol, never per batch. A single bad frame must cost
                # one row, not the whole cycle.
                log.warning("%s: could not build quote (%s)", symbol, exc)
                continue
            if quote is not None:
                quotes[symbol] = quote

        missing = set(symbols) - set(quotes)
        if missing:
            log.info("no intraday data for %d symbol(s): %s", len(missing), sorted(missing))
        return quotes

    def _download(self, tickers: list[str], retry_on_empty: bool) -> pd.DataFrame | None:
        """Batch download with backoff, treating an empty frame as a failure.

        yfinance catches its own HTTP errors and returns an empty DataFrame
        rather than raising, so retrying only on exceptions would never
        actually retry a rate-limited or blocked request -- the backoff would
        be dead code for the failure it exists to handle. During a session an
        empty response therefore counts as a failure worth retrying; outside
        one it is simply the truth.
        """
        attempts = len(_BACKOFF_SECONDS) + 1
        for attempt in range(attempts):
            reason: str | None = None
            try:
                frame = yf.download(
                    tickers=" ".join(tickers),
                    period=_HISTORY_PERIOD,
                    interval="1m",
                    group_by="ticker",
                    threads=True,
                    progress=False,
                )
                if frame is not None and not frame.empty:
                    return frame
                if not retry_on_empty:
                    return frame
                reason = "empty response during an open session"
            except Exception as exc:
                reason = str(exc)

            if attempt == attempts - 1:
                log.error("yfinance unavailable after %d attempts (%s); "
                          "skipping this cycle", attempts, reason)
                return None
            delay = _BACKOFF_SECONDS[attempt]
            log.warning("yfinance download failed (%s); retrying in %ss", reason, delay)
            time.sleep(delay)
        return None

    @staticmethod
    def _frame_for(raw: pd.DataFrame, ticker: str) -> pd.DataFrame | None:
        """Pull one ticker's OHLCV out of a grouped download.

        yfinance returns MultiIndex columns for a multi-ticker request and
        flat columns for a single-ticker one, so both shapes have to be
        handled or the poller breaks the moment the hot set drops to one
        symbol.
        """
        if isinstance(raw.columns, pd.MultiIndex):
            if ticker not in raw.columns.get_level_values(0):
                return None
            frame = raw[ticker]
        else:
            frame = raw
        frame = frame.dropna(how="all")
        return frame if not frame.empty else None

    @staticmethod
    def _naive_ist_index(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
        if index.tz is not None:
            return index.tz_convert(IST).tz_localize(None)
        return index

    def _build_quote(
        self,
        raw: pd.DataFrame,
        ticker: str,
        symbol: str,
        session_day: date,
        prev_day: date,
    ) -> Quote | None:
        frame = self._frame_for(raw, ticker)
        if frame is None:
            return None

        stamps = self._naive_ist_index(frame.index)
        days = stamps.date

        today_rows = frame[days == session_day]
        if today_rows.empty:
            # Before the open, or a symbol that did not trade. There is no
            # current price, and inventing one from an older session is the
            # one thing this codebase will not do.
            return None

        prev_rows = frame[days == prev_day]
        if prev_rows.empty:
            # Without the correct previous close every return computed today
            # would be wrong. Omitting the symbol costs one row; guessing
            # costs the user's trust in every number on the screen.
            log.warning(
                "%s: no bars for previous trading day %s; omitting rather than "
                "computing returns off the wrong session", symbol, prev_day
            )
            return None

        as_of = pd.Timestamp(stamps[days == session_day][-1]).to_pydatetime()

        return Quote(
            symbol=symbol,
            price=float(today_rows["Close"].iloc[-1]),
            open=float(today_rows["Open"].iloc[0]),
            previous_close=float(prev_rows["Close"].iloc[-1]),
            volume=float(today_rows["Volume"].sum()),
            as_of=as_of,
            source=self.name,
            freshness=Freshness.DELAYED,
        )
