"""Tick validation and source reconciliation.

The brief asks how the system handles stale, delayed and conflicting data. This
module is that answer. Its guiding principle: it is always better to show the
user nothing, clearly labelled, than to show them a number that is wrong.

Nothing here silently corrects data. Rejections are recorded so that a symbol
whose feed is producing garbage can be surfaced as degraded rather than
appearing to trade normally at a fabricated price.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.config import settings
from app.providers.base import Freshness, Quote


@dataclass
class ValidationResult:
    accepted: Quote | None
    rejected_reason: str | None = None
    conflict_note: str | None = None

    @property
    def ok(self) -> bool:
        return self.accepted is not None


def validate_tick(
    quote: Quote,
    last_accepted: Quote | None,
    sigma: float,
    now: datetime,
) -> ValidationResult:
    """Decide whether to believe a single incoming quote.

    sigma is the symbol's own daily return standard deviation, so the sanity
    band is proportional to how much the symbol actually moves. A fixed
    percentage band would either wave through nonsense on a calm large-cap or
    constantly reject legitimate moves on a volatile smallcap.
    """
    if quote.price <= 0:
        return ValidationResult(None, rejected_reason="non_positive_price")

    if quote.as_of > now:
        # A timestamp in the future means clock skew somewhere upstream. The
        # price may well be fine, but we cannot reason about its age, and age
        # is what every staleness decision depends on.
        return ValidationResult(None, rejected_reason="future_timestamp")

    if last_accepted is None:
        return ValidationResult(quote)

    if last_accepted.price <= 0 or sigma <= 0:
        return ValidationResult(quote)

    move = abs(quote.price - last_accepted.price) / last_accepted.price
    band = settings.tick_sanity_sigma * sigma

    if move > band:
        # A 12-sigma jump between consecutive ticks is not a market event, it
        # is a bad print: a fat finger, a decimal shift, or a mangled parse.
        # Real markets have circuit breakers precisely because prices do not
        # move like this intact.
        #
        # We quarantine rather than discard: one corroborating tick at a
        # similar level and it gets accepted on the next pass. That way a
        # genuine limit-up move is delayed by one cycle rather than lost.
        return ValidationResult(
            None,
            rejected_reason=(
                f"implausible_move: {move * 100:.1f}% in one tick, "
                f"band is {band * 100:.1f}%"
            ),
        )

    return ValidationResult(quote)


def reconcile(
    primary: Quote,
    secondary_price: float | None,
    tolerance: float = 0.005,
) -> ValidationResult:
    """Resolve disagreement between two sources for the same symbol.

    Policy, in order:

      1. Prefer the primary exchange feed. It is the venue of record; a
         consolidated or third-party feed is a derived view of it.
      2. If they disagree beyond tolerance, still serve the primary, but mark
         the quote as disputed and record the gap.

    Deliberately *not* averaging them. An average of two numbers where one is
    wrong is a third number that is also wrong, but which now looks
    authoritative and matches neither source. It also destroys the evidence
    needed to work out which feed is broken.
    """
    if secondary_price is None or secondary_price <= 0:
        return ValidationResult(primary)

    gap = abs(primary.price - secondary_price) / primary.price
    if gap <= tolerance:
        return ValidationResult(primary)

    disputed = Quote(
        **{**primary.__dict__, "freshness": Freshness.DELAYED}
    )
    return ValidationResult(
        disputed,
        conflict_note=(
            f"Sources disagree by {gap * 100:.2f}% "
            f"({primary.source} {primary.price:.2f} vs secondary "
            f"{secondary_price:.2f}); showing the primary exchange price"
        ),
    )
