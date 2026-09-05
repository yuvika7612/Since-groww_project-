"""End-to-end demo scenarios.

These are the tests that stop a refactor quietly breaking the presentation.
Every other test here checks a component; these check that the three things
the product actually claims still happen when the whole stack runs:

    a quiet session produces silence
    a market-wide selloff collapses into one headline plus the exception
    a split is reported as a split, not as the worst crash on record

They are integration tests by necessity and they run against the real seeded
database and fixture, which they mutate -- running a scenario clears events
and watermarks by design. They skip rather than fail on a clone that has not
been seeded yet, because a skipped test says "not set up" and a failing one
would wrongly say "broken".
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.models import Symbol
from workers import ingest

FIXTURE = Path(__file__).resolve().parent.parent / "data" / "session.jsonl"


def _seeded() -> bool:
    if not FIXTURE.exists():
        return False
    try:
        with SessionLocal() as session:
            return session.query(Symbol).count() > 0
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _seeded(),
    reason="run scripts/seed.py and scripts/record_fixture.py first",
)


@pytest.fixture
def demo(monkeypatch):
    """The real stack, with the poll loop driven by hand.

    The lifespan would otherwise start the worker in a thread and race the
    assertions; stubbing run() and calling _cycle() directly makes each
    scenario deterministic.
    """
    monkeypatch.setattr(ingest, "run", lambda: None)
    # Pre-set so _cycle returns immediately instead of sleeping out its poll
    # interval at the end of every call.
    stop = threading.Event()
    stop.set()
    monkeypatch.setattr(ingest, "_shutdown", stop)

    from app.main import app

    with TestClient(app) as client:
        token = client.post(
            "/api/auth/dev-login", json={"email": "demo@since.app"}
        ).json()["token"]
        client.headers.update({"Authorization": f"Bearer {token}"})
        yield client


def run_scenario(client: TestClient, key: str, cycles: int = 3) -> dict:
    response = client.post(f"/api/debug/scenarios/{key}")
    assert response.status_code == 200, response.text
    # The scenario seeks the clock and clears the cache; these cycles are the
    # poller catching up to the new moment.
    for _ in range(cycles):
        ingest._cycle()
    digest = client.get("/api/digest")
    assert digest.status_code == 200, digest.text
    return digest.json()


def symbols_of(rows: list[dict]) -> set[str]:
    return {row["symbol"] for row in rows}


def test_a_quiet_session_produces_silence(demo):
    """The output most watchlists cannot produce, and the correct answer most
    days. If this ever fails, the product's central claim is broken."""
    digest = run_scenario(demo, "quiet_day")

    assert digest["needs_attention"] == [], (
        "a quiet session surfaced rows: "
        f"{[(r['symbol'], r['primary_reason']) for r in digest['needs_attention']]}"
    )
    assert digest["degraded"] == []
    assert len(digest["quiet"]) > 0, "the symbols should still be there, just quiet"


def test_a_market_wide_selloff_collapses_to_the_exception(demo):
    """Twelve red rows are one piece of information.

    The large caps fall by their own beta, so their residuals are ~0 and they
    collapse into the headline. IRFC stands still, which its beta says it
    should not have, so it is the only row carrying new information.
    """
    digest = run_scenario(demo, "market_selloff")

    surfaced = symbols_of(digest["needs_attention"])
    quiet = symbols_of(digest["quiet"])

    assert digest["market"] is not None
    assert digest["market"]["is_market_wide"], "the selloff was not detected as broad"
    assert "IRFC" in surfaced, f"the holdout did not surface; surfaced={surfaced}"

    followers = {"HDFCBANK", "ICICIBANK", "RELIANCE", "SBIN"}
    assert followers <= quiet, (
        f"correlated movers were not collapsed: {followers - quiet} still surfaced"
    )

    irfc = next(r for r in digest["needs_attention"] if r["symbol"] == "IRFC")
    assert any(e["type"] == "idiosyncratic_move" for e in irfc["events"])


def test_a_split_is_reported_as_a_split(demo):
    """The alert that would end the product's credibility.

    The quoted price drops 80% overnight against a previous_close the feed has
    not restated. Everything downstream must survive that: no abnormal move,
    no overnight gap, and above all no new 52-week low from stored levels left
    in old shares.
    """
    digest = run_scenario(demo, "split")

    rows = digest["needs_attention"] + digest["quiet"] + digest["degraded"]
    tcs = next(r for r in rows if r["symbol"] == "TCS")
    types = {event["type"] for event in tcs["events"]}

    assert "corporate_action" in types, f"the split was not explained; events={types}"
    assert "abnormal_move" not in types, "the split was reported as a price move"
    assert "gap" not in types, "the split was reported as an overnight gap"
    assert "range_break" not in types, "the split produced a false 52-week break"

    # The row must not claim the holding lost 80% of its value either.
    assert abs(tcs["change_since_seen"]) < 0.1, (
        f"change_since_seen is {tcs['change_since_seen']:.3f}; "
        "previous_close was not restated"
    )
