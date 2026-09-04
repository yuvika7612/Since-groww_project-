"""Deterministic replay provider.

Plays a recorded or synthesised session against a virtual clock, and can inject
specific faults on cue.

The fault catalogue is the point. Each one corresponds to a real failure mode
this system claims to handle, and each can be triggered at a chosen moment so
the handling can actually be shown rather than merely asserted:

    outage          the feed stops responding for a window
    bad_tick        a single wildly wrong print, as happens with fat fingers
                    and decimal errors on real exchanges
    conflict        two sources disagree about the same symbol
    split           a corporate action that moves the quoted price 80%
    frozen          a feed that keeps responding but stops updating, which is
                    more dangerous than an outage because it looks healthy
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from app.providers.base import Freshness, MarketDataProvider, Quote


@dataclass
class Fault:
    """A scheduled failure."""

    kind: str  # outage | bad_tick | conflict | split | frozen
    symbol: str | None  # None means every symbol
    start: datetime
    end: datetime | None = None
    magnitude: float = 1.0

    def active_at(self, when: datetime) -> bool:
        if when < self.start:
            return False
        if self.end is None:
            # Instantaneous faults fire for a single virtual minute.
            return when < self.start + timedelta(minutes=1)
        return when < self.end


class ReplayProvider(MarketDataProvider):
    name = "replay"

    def __init__(
        self,
        fixture_path: str | Path,
        speed: float = 60.0,
        start_at: datetime | None = None,
    ):
        """
        speed: virtual seconds elapsed per wall-clock second. At 60, a full
        6h15m NSE session replays in about six minutes, which is roughly the
        length of a demo.
        """
        self.speed = speed
        self._frames: list[dict] = []
        self._faults: list[Fault] = []
        self._load(Path(fixture_path))
        self._virtual_start = start_at or (
            datetime.fromisoformat(self._frames[0]["as_of"])
            if self._frames
            else datetime.now()
        )
        self._wall_start = datetime.now()
        # Set by inject_conflict; read by the validator to test its resolution
        # policy against genuinely disagreeing sources.
        self._secondary: dict[str, float] = {}

    def _load(self, path: Path) -> None:
        if not path.exists():
            return
        with path.open() as handle:
            for line in handle:
                line = line.strip()
                if line:
                    self._frames.append(json.loads(line))

    # --- Virtual clock --------------------------------------------------

    def now(self) -> datetime:
        elapsed = (datetime.now() - self._wall_start).total_seconds()
        return self._virtual_start + timedelta(seconds=elapsed * self.speed)

    def seek(self, when: datetime) -> None:
        """Jump the virtual clock. Used in the demo to skip to a fault."""
        self._virtual_start = when
        self._wall_start = datetime.now()

    # --- Fault scheduling -----------------------------------------------

    def schedule(self, fault: Fault) -> None:
        self._faults.append(fault)

    def inject_now(self, kind: str, symbol: str | None = None, magnitude: float = 1.0,
                   duration_minutes: int | None = None) -> None:
        """Fire a fault immediately. Wired to a debug endpoint for the demo."""
        start = self.now()
        end = start + timedelta(minutes=duration_minutes) if duration_minutes else None
        self.schedule(Fault(kind=kind, symbol=symbol, start=start, end=end,
                            magnitude=magnitude))

    def _faults_for(self, symbol: str, when: datetime) -> list[Fault]:
        return [
            f for f in self._faults
            if f.active_at(when) and (f.symbol is None or f.symbol == symbol)
        ]

    # --- Fetch ----------------------------------------------------------

    def fetch(self, symbols: list[str]) -> dict[str, Quote]:
        when = self.now()
        wanted = set(symbols)
        out: dict[str, Quote] = {}

        for symbol in wanted:
            frame = self._frame_for(symbol, when)
            if frame is None:
                continue

            price = float(frame["price"])
            open_price = float(frame.get("open", price))
            as_of = when
            drop = False

            for fault in self._faults_for(symbol, when):
                if fault.kind == "outage":
                    # Omit entirely. The consumer must handle a symbol simply
                    # not being in the response.
                    drop = True
                elif fault.kind == "bad_tick":
                    price *= fault.magnitude or 1.4
                elif fault.kind == "frozen":
                    # Still responds, but as_of stops advancing. This is the
                    # dangerous one: the payload looks perfectly healthy and
                    # only the timestamp reveals the problem.
                    as_of = fault.start
                elif fault.kind == "split":
                    # Every price in the quote is in new shares on an ex-date,
                    # not just the last trade. Dividing price alone leaves the
                    # opening print in old shares, and overnight_gap() then
                    # compares it against a restated previous_close and
                    # reports a 400% gap.
                    #
                    # previous_close is deliberately left in old shares: a
                    # real feed does not restate it, and handling that is the
                    # whole point of the exercise.
                    ratio = fault.magnitude or 5.0
                    price /= ratio
                    open_price /= ratio
                elif fault.kind == "conflict":
                    self._secondary[symbol] = price * (1 + 0.03 * fault.magnitude)

            if drop:
                continue

            out[symbol] = Quote(
                symbol=symbol,
                price=round(price, 2),
                open=open_price,
                previous_close=float(frame.get("previous_close", price)),
                volume=float(frame.get("volume", 0.0)),
                as_of=as_of,
                source=self.name,
                freshness=Freshness.LIVE,
            ).aged(when, stale_after_seconds=90)

        return out

    def secondary_quotes(self) -> dict[str, float]:
        """Prices from the notional second source, for conflict testing."""
        return dict(self._secondary)

    def _frame_for(self, symbol: str, when: datetime) -> dict | None:
        """Most recent frame for this symbol at or before the virtual time.

        Linear scan is fine here: fixtures are a single session and this runs
        in a demo, not in production. Noting the tradeoff rather than
        pre-optimising it.
        """
        best = None
        for frame in self._frames:
            if frame["symbol"] != symbol:
                continue
            if datetime.fromisoformat(frame["as_of"]) <= when:
                best = frame
            else:
                break
        return best
