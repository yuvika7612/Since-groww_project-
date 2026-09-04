"""Demo controls. Replay provider only.

This is what the live demo is driven from. The interesting behaviour of this
system is what it does when a feed breaks, a source disagrees, or a split
lands, and none of that can be produced on cue from a live market. These
endpoints make each failure reproducible in front of an audience.

Every route 404s unless the replay provider is active, so the fault injector
cannot exist in a deployment pointed at real data.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status

from app.config import settings
from app.market.calendar import IST
from app.providers.factory import provider
from app.providers.replay import ReplayProvider
from app.schemas import InjectFaultRequest, ScenarioFault, ScenarioOut, SeekRequest

router = APIRouter(prefix="/debug", tags=["debug"])

# The session the bundled fixture replays. Offset-aware so every timestamp
# leaving the API carries one, like every other datetime in the schema.
SESSION_DAY = datetime(2026, 9, 4, tzinfo=IST)


def replay_only() -> ReplayProvider:
    if settings.market_provider != "replay" or not isinstance(provider, ReplayProvider):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return provider


# Each scenario is a clock position plus the faults that make it happen, so
# the demo is a sequence of two calls rather than a script that has to be
# remembered under pressure.
SCENARIOS: list[ScenarioOut] = [
    ScenarioOut(
        key="quiet_day",
        title="A quiet session",
        description=(
            "Nothing meaningful happened. The output most watchlists cannot "
            "produce, and the correct answer most days."
        ),
        seek_to=SESSION_DAY.replace(hour=11, minute=0),
        faults=[],
    ),
    ScenarioOut(
        key="market_selloff",
        title="Market-wide selloff",
        description=(
            "Twelve red rows are one piece of information. Correlated moves "
            "collapse into a headline; only what moved differently survives."
        ),
        seek_to=SESSION_DAY.replace(hour=14, minute=5),
        faults=[],
    ),
    ScenarioOut(
        key="split",
        title="1:5 stock split",
        description=(
            "The alert that would end the product's credibility. Price drops "
            "80% overnight and the user is told what happened, not that they "
            "lost their money."
        ),
        seek_to=SESSION_DAY.replace(hour=9, minute=20),
        faults=[ScenarioFault(kind="split", symbol="TCS", magnitude=5.0)],
    ),
    ScenarioOut(
        key="feed_outage",
        title="Feed outage",
        description=(
            "The symbol stops responding. Its row is separated out as our "
            "failure rather than ranked as market news."
        ),
        seek_to=SESSION_DAY.replace(hour=12, minute=30),
        faults=[
            ScenarioFault(kind="outage", symbol="INFY", duration_minutes=30)
        ],
    ),
    ScenarioOut(
        key="bad_tick",
        title="Bad print",
        description=(
            "A 40% jump in one tick is a fat finger or a decimal shift. "
            "Quarantined against the symbol's own sigma band, not discarded."
        ),
        seek_to=SESSION_DAY.replace(hour=13, minute=0),
        faults=[
            ScenarioFault(kind="bad_tick", symbol="RELIANCE", magnitude=1.4)
        ],
    ),
    ScenarioOut(
        key="frozen_feed",
        title="Frozen feed",
        description=(
            "More dangerous than an outage: the payload looks perfectly "
            "healthy and only the timestamp reveals the problem."
        ),
        seek_to=SESSION_DAY.replace(hour=14, minute=30),
        faults=[
            ScenarioFault(kind="frozen", symbol="HDFCBANK", duration_minutes=60)
        ],
    ),
]


@router.get("/scenarios", response_model=list[ScenarioOut])
def list_scenarios(_: ReplayProvider = Depends(replay_only)) -> list[ScenarioOut]:
    return SCENARIOS


@router.post("/inject-fault")
def inject_fault(
    payload: InjectFaultRequest,
    replay: ReplayProvider = Depends(replay_only),
) -> dict:
    replay.inject_now(
        kind=payload.kind,
        symbol=payload.symbol,
        magnitude=payload.magnitude,
        duration_minutes=payload.duration_minutes,
    )
    return {
        "injected": payload.kind,
        "symbol": payload.symbol,
        "at": replay.now().replace(tzinfo=IST).isoformat(),
    }


@router.post("/seek")
def seek(payload: SeekRequest, replay: ReplayProvider = Depends(replay_only)) -> dict:
    target = payload.to
    if target.tzinfo is not None:
        target = target.astimezone(IST).replace(tzinfo=None)
    replay.seek(target)
    return {"now": replay.now().replace(tzinfo=IST).isoformat()}
