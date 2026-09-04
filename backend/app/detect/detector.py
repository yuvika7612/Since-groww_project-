"""Change detection.

Runs once per symbol per tick, shared by every user watching that symbol. This
is the half of the system that scales with the number of *instruments*, which
is bounded at a few thousand, rather than with the number of *users*, which is
not.

Personalisation happens later and cheaply, in digest/service.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from app.config import settings
from app.detect.events import ChangeEvent, EventType
from app.detect.signals import (
    SymbolStatistics,
    log_saturate,
    overnight_gap,
    range_break,
    relative_volume,
    residual_return,
    saturate,
    z_score,
)


@dataclass
class TickContext:
    """Everything the detector needs to evaluate one symbol at one instant."""

    symbol: str
    price: float
    previous_close: float
    open_price: float
    volume_so_far: float
    session_fraction: float  # share of a normal day's volume expected by now
    index_return: float  # today's return of the reference index
    observed_at: datetime
    session_date: date

    @property
    def day_return(self) -> float:
        if self.previous_close <= 0:
            return 0.0
        return (self.price - self.previous_close) / self.previous_close


def detect(ctx: TickContext, stats: SymbolStatistics) -> list[ChangeEvent]:
    """Evaluate one tick and return the events it triggers.

    Ordering note: the caller must have already applied corporate action
    adjustment to previous_close. If a split has not been adjusted for, the
    day_return here is nonsense and every downstream signal inherits that.
    """
    events: list[ChangeEvent] = []

    if not stats.is_scorable:
        # Suspended, newly listed, or illiquid. We have no basis for calling
        # anything unusual, so we say nothing rather than guessing.
        return events

    z = z_score(ctx.day_return, stats)
    rvol = relative_volume(ctx.volume_so_far, stats, ctx.session_fraction)
    residual = residual_return(ctx.day_return, ctx.index_return, stats)

    def emit(etype: EventType, severity: float, explanation: str, **payload) -> None:
        events.append(
            ChangeEvent(
                symbol=ctx.symbol,
                type=etype,
                severity=round(min(max(severity, 0.0), 1.0), 4),
                occurred_at=ctx.observed_at,
                session_date=ctx.session_date,
                explanation=explanation,
                payload=payload,
            )
        )

    # --- Abnormal move --------------------------------------------------
    # Volume acts as a multiplier rather than a gate. A large move on thin
    # volume is usually a thin order book rather than information, so it is
    # damped but not suppressed entirely: it is still true that the price
    # moved, and the user may still care.
    if abs(z) >= settings.z_threshold:
        confidence = 0.6 + 0.4 * log_saturate(rvol, cap=5.0)
        emit(
            EventType.ABNORMAL_MOVE,
            saturate(z, cap=3.0) * confidence,
            f"{'Up' if z > 0 else 'Down'} {abs(ctx.day_return) * 100:.1f}%, "
            f"{abs(z):.1f} sigma versus its own 30-day range",
            z=round(z, 3),
            day_return=round(ctx.day_return, 5),
            rvol=round(rvol, 2),
        )

    # --- Idiosyncratic move ---------------------------------------------
    # The differentiator. If the index fell 2% and this stock fell 2%, the
    # user learned nothing from the row that the market summary had not
    # already told them. Only the part the market fails to explain earns a
    # slot. This is also what surfaces a stock that stayed flat on a red day,
    # which every conventional watchlist renders as an unremarkable grey row.
    if abs(residual) >= settings.residual_threshold:
        direction = "outperformed" if residual > 0 else "underperformed"
        emit(
            EventType.IDIOSYNCRATIC_MOVE,
            saturate(residual, cap=0.06),
            f"Moved against the market: {direction} the index by "
            f"{abs(residual) * 100:.1f}% after adjusting for beta "
            f"{stats.beta_60d:.2f}",
            residual=round(residual, 5),
            index_return=round(ctx.index_return, 5),
            beta=round(stats.beta_60d, 3),
        )

    # --- Volume spike ---------------------------------------------------
    # Emitted independently of price, because heavy volume with a flat price
    # means accumulation or distribution: someone large is transacting and the
    # price has not resolved yet. That is worth knowing before it resolves.
    if rvol >= settings.rvol_threshold:
        emit(
            EventType.VOLUME_SPIKE,
            log_saturate(rvol, cap=8.0),
            f"{rvol:.1f}x its normal volume for this point in the session",
            rvol=round(rvol, 2),
            volume_so_far=ctx.volume_so_far,
        )

    # --- Overnight gap --------------------------------------------------
    gap = overnight_gap(ctx.open_price, ctx.previous_close)
    if abs(gap) >= settings.gap_threshold:
        emit(
            EventType.GAP,
            saturate(gap, cap=0.08),
            f"Gapped {'up' if gap > 0 else 'down'} {abs(gap) * 100:.1f}% at the "
            f"open, so this reacted to news outside market hours",
            gap=round(gap, 5),
            open_price=ctx.open_price,
        )

    # --- 52-week range break --------------------------------------------
    # Not statistically special, but users anchor on these levels, and a
    # watchlist that ignores what its users actually look at is solving the
    # wrong problem.
    brk = range_break(ctx.price, stats)
    if brk != 0:
        emit(
            EventType.RANGE_BREAK,
            0.5 + 0.5 * saturate(brk, cap=0.05),
            f"New 52-week {'high' if brk > 0 else 'low'} at {ctx.price:.2f}",
            distance=round(brk, 5),
            level=stats.high_52w if brk > 0 else stats.low_52w,
        )

    return events


def dedupe(events: list[ChangeEvent], seen_keys: set[str]) -> list[ChangeEvent]:
    """Drop events already emitted this session at the same severity bucket.

    Called with the set of keys already persisted for the session. Mutates
    seen_keys so a single batch cannot emit the same event twice either.
    """
    fresh = []
    for event in events:
        key = event.dedupe_key
        if key in seen_keys:
            continue
        seen_keys.add(key)
        fresh.append(event)
    return fresh
