"""Change event types.

A deliberate design choice: the system does not produce a single opaque
"interestingness score". It produces typed events, each carrying the numbers
that caused it and a sentence explaining itself.

The reason is defensibility. When a user asks why a stock is at the top of
their digest, "the model decided" is not an acceptable answer in a product that
handles money. "Moved 2.1 sigma on 3.4x normal volume" is. Every event below
can be traced back to arithmetic on stored inputs and reproduced exactly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any


class EventType(str, Enum):
    # Market events: something happened to the instrument.
    ABNORMAL_MOVE = "abnormal_move"
    IDIOSYNCRATIC_MOVE = "idiosyncratic_move"
    VOLUME_SPIKE = "volume_spike"
    GAP = "gap"
    RANGE_BREAK = "range_break"
    CORPORATE_ACTION = "corporate_action"

    # Personal events: something happened relative to this user's position.
    CROSSED_COST_BASIS = "crossed_cost_basis"

    # Data quality events: something happened to our knowledge of the
    # instrument. Surfaced to the user rather than hidden, because silently
    # showing a stale price as if it were live is the failure mode that
    # destroys trust in a market product.
    STALE_DATA = "stale_data"
    SOURCE_CONFLICT = "source_conflict"


# Events that describe the market rather than our plumbing. Only these compete
# for slots in the attention budget; data-quality events are surfaced inline on
# the affected row instead.
MARKET_EVENTS = {
    EventType.ABNORMAL_MOVE,
    EventType.IDIOSYNCRATIC_MOVE,
    EventType.VOLUME_SPIKE,
    EventType.GAP,
    EventType.RANGE_BREAK,
    EventType.CORPORATE_ACTION,
    EventType.CROSSED_COST_BASIS,
}

# A corporate action is never a price signal. A 1:5 split drops the quoted
# price by 80% overnight; a naive diff reports that as the most urgent thing
# that has ever happened to the user's portfolio. One alert like that and the
# user stops believing the product. So splits are always reported as their own
# event type, and the price series is back-adjusted before any return is
# computed from it.
NEVER_SCORED_AS_PRICE_MOVE = {EventType.CORPORATE_ACTION}


@dataclass
class ChangeEvent:
    symbol: str
    type: EventType
    severity: float  # normalised to [0, 1] so types are comparable
    occurred_at: datetime
    session_date: date
    explanation: str  # shown verbatim in the UI
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def dedupe_key(self) -> str:
        """Identity for a single logical occurrence.

        A stock drifting from 1.6 to 1.9 sigma over ten minutes is one event
        that got slightly worse, not thirty events. Bucketing the magnitude
        means an event re-fires only when it escalates to a materially
        different level, which is also the only time a user would want to be
        told again.
        """
        bucket = int(self.severity * 4)  # quarters of the severity range
        return f"{self.symbol}:{self.type.value}:{self.session_date.isoformat()}:{bucket}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "type": self.type.value,
            "severity": round(self.severity, 4),
            "occurred_at": self.occurred_at.isoformat(),
            "session_date": self.session_date.isoformat(),
            "explanation": self.explanation,
            "payload": self.payload,
        }
