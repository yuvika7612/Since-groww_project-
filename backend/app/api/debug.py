"""Demo controls. Replay provider only.

This is what the live demo is driven from. The interesting behaviour of this
system is what it does when a feed breaks, a source disagrees, or a split
lands, and none of that can be produced on cue from a live market. These
endpoints make each failure reproducible in front of an audience.

Every route 404s unless the replay provider is active, so the fault injector
cannot exist in a deployment pointed at real data.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status

from app.cache import cache
from app.config import settings
from app.db import SessionLocal
from app.models import ChangeEventRow, CorporateAction, UserSymbolSeen
from app.market.calendar import IST
from app.providers.factory import provider
from app.providers.replay import Fault, ReplayProvider
from workers import nightly
from app.schemas import InjectFaultRequest, ScenarioFault, ScenarioOut, SeekRequest

router = APIRouter(prefix="/debug", tags=["debug"])

# The session the bundled fixture replays. Offset-aware so every timestamp
# leaving the API carries one, like every other datetime in the schema.
SESSION_DAY = datetime(2026, 9, 4, tzinfo=IST)

# The split scenario injects a price fault; without a matching ex-date the
# worker has nothing to explain the 80% drop with and would report the very
# false crash this project exists to prevent.
SPLIT_DEMO_SYMBOL = "TCS"
SPLIT_DEMO_RATIO = 5.0

# Virtual time between the seek and a fault that *disrupts* an otherwise
# working feed, so the poller gets one clean tick to establish a sanity
# baseline first. Without it, seeking clears the quote cache, the broken tick
# arrives with nothing to be compared against, validate_tick accepts it
# unconditionally, and the bad-print demo shows a 40% jump being ranked
# instead of quarantined.
FAULT_LEAD_IN = timedelta(seconds=30)

# A split is not a disruption, it is a property of the session: on an ex-date
# the price is quoted in new shares from the opening bell. Delaying it would
# leave one cycle where previous_close has been restated but the price has
# not, which reads as a 400% rally.
IMMEDIATE_FAULTS = {"split"}


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
        # Duration matters: an instantaneous fault fires for one virtual
        # minute, after which the quoted price snaps back to old shares while
        # previous_close stays restated, and the detector reports a 400%
        # rally. A split does not last a minute, it lasts the session.
        faults=[
            ScenarioFault(
                kind="split", symbol="TCS", magnitude=5.0, duration_minutes=400
            )
        ],
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


@router.post("/scenarios/{key}")
def run_scenario(key: str, replay: ReplayProvider = Depends(replay_only)) -> dict:
    """Seek the clock and inject the faults for one preset, in one call.

    The demo is driven from here, so it has to be a single button press with
    nothing to remember under pressure.

    Previously scheduled faults are cleared first. Without that, running the
    split scenario and then the quiet day leaves the split still firing, and
    the "quiet day" that is supposed to show silence shows a corporate action
    instead -- which is the one moment in the demo where a stale fault is most
    obvious and least recoverable.
    """
    scenario = next((s for s in SCENARIOS if s.key == key), None)
    if scenario is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Unknown scenario {key}. Try: {', '.join(s.key for s in SCENARIOS)}",
        )

    replay._faults.clear()
    replay._secondary.clear()
    # The clock is about to move, which makes every cached price a
    # description of a different moment. See Cache.clear_quotes.
    cache.clear_quotes()

    # Events already detected for this session are discarded, because a
    # scenario seeks the clock *backwards* as often as forwards and a detected
    # event does not un-happen when time rewinds. Without this, running the
    # selloff and then the quiet day shows the quiet day still carrying five
    # alerts from a future that has not happened yet -- and the quiet day is
    # the scenario whose entire point is that the screen says nothing.
    #
    # Safe only because this is replay-only and the fixture is deterministic:
    # the poller re-detects everything true at the new clock position within
    # one cycle.
    # Read watermarks go with them, for the same reason. A scenario seeks the
    # clock backwards as readily as forwards, and a watermark stamped 14:26 by
    # the previous run silently marks every event re-detected at 14:05 as
    # already read -- so the second run of the demo shows an empty screen and
    # looks broken at exactly the wrong moment.
    #
    # This is a reset, not a correction: the watermark logic itself is right,
    # and outside replay a clock never runs backwards.
    with SessionLocal() as session:
        removed = session.query(ChangeEventRow).delete()
        session.query(UserSymbolSeen).delete()

        # The corporate action is provisioned per scenario rather than seeded
        # onto the replayed session, because an ex-date on that session makes
        # every scenario carry a split -- and the quiet day, whose entire
        # point is that the screen says nothing, would correctly but
        # uselessly report one.
        #
        # seed.py dates the real split 60 days back, where it exercises the
        # nightly back-adjustment. This adds a same-day one only while the
        # split scenario is the one being shown.
        session.query(CorporateAction).filter(
            CorporateAction.symbol == SPLIT_DEMO_SYMBOL,
            CorporateAction.ex_date == SESSION_DAY.date(),
        ).delete()
        if scenario.key == "split":
            session.add(
                CorporateAction(
                    symbol=SPLIT_DEMO_SYMBOL,
                    ex_date=SESSION_DAY.date(),
                    action_type="split",
                    ratio=SPLIT_DEMO_RATIO,
                    amount=0.0,
                )
            )
        session.commit()

    # Statistics are recomputed because the corporate action just changed, and
    # this is the remedy workers/ingest.py:_apply_corporate_action documents
    # for exactly this situation: an action ingested after the last nightly
    # run leaves high_52w and low_52w in old shares. Skip it and the split
    # scenario prices TCS at 758 against a 52-week low of 3780 and reports a
    # new 52-week low -- the precise false alarm the product exists to
    # prevent, produced by the demo that is supposed to disprove it.
    nightly.run()

    target = scenario.seek_to
    if target.tzinfo is not None:
        target = target.astimezone(IST).replace(tzinfo=None)
    replay.seek(target)

    # Faults start a little after the seek, not at it. Seeking clears the
    # quote cache, so the first tick afterwards has no last_accepted to be
    # measured against and validate_tick accepts it unconditionally -- which
    # is right on a genuine cold start and wrong here, because it lets the
    # bad-print fault through as the baseline and the demo shows a 40% jump
    # being ranked rather than quarantined. One clean cycle first gives the
    # sanity band something to work from.
    now = replay.now()
    for fault in scenario.faults:
        start = now if fault.kind in IMMEDIATE_FAULTS else now + FAULT_LEAD_IN
        end = (
            start + timedelta(minutes=fault.duration_minutes)
            if fault.duration_minutes
            else None
        )
        replay.schedule(
            Fault(
                kind=fault.kind,
                symbol=fault.symbol,
                start=start,
                end=end,
                magnitude=fault.magnitude,
            )
        )

    return {
        "scenario": scenario.key,
        "title": scenario.title,
        "now": replay.now().replace(tzinfo=IST).isoformat(),
        "faults_injected": len(scenario.faults),
        "events_cleared": removed,
    }
