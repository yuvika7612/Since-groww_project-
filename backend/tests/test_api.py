"""API tests.

The SSE subscription-leak test at the bottom is the one that earns its keep.
That leak is invisible from the outside: the stream works, the client is
happy, and the only symptom is a subscriber list that grows on every browser
reconnect until the process dies. Nothing but an assertion catches it.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.digest import _stream, _watched_symbols
from app.cache import InMemoryCache
from app.db import get_session
from app.market.calendar import IST
from app.models import Base, Symbol


@pytest.fixture
def client(monkeypatch):
    """The app wired to a throwaway database and a fresh cache.

    The cache singleton is patched in every module that imported it by name,
    because `from app.cache import cache` binds the object, not the module
    attribute.
    """
    # StaticPool keeps every session on one connection. Without it each new
    # connection to ":memory:" opens its own empty database and the tables
    # created below are invisible to the request handlers.
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    with factory() as setup:
        for symbol, name in [
            ("TCS", "Tata Consultancy Services"),
            ("INFY", "Infosys"),
            ("HDFCBANK", "HDFC Bank"),
            ("^NSEI", "NIFTY 50"),
        ]:
            setup.add(Symbol(symbol=symbol, name=name, exchange="NSE", benchmark="^NSEI"))
        setup.commit()

    fresh = InMemoryCache()
    import app.api.digest as digest_module
    import app.api.watchlists as watchlists_module
    import app.main as main_module

    for module in (digest_module, watchlists_module, main_module):
        monkeypatch.setattr(module, "cache", fresh)

    from app.main import app

    def override():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_session] = override
    with TestClient(app) as test_client:
        test_client.cache = fresh
        yield test_client
    app.dependency_overrides.clear()


def auth(client: TestClient, email: str = "demo@example.com") -> dict:
    token = client.post("/api/auth/dev-login", json={"email": email}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


# --- Auth -------------------------------------------------------------------


def test_missing_or_invalid_credentials_are_401_never_403(client):
    """401 says "I don't know who you are". 403 would confirm the row exists."""
    assert client.get("/api/watchlists").status_code == 401
    assert client.get(
        "/api/watchlists", headers={"Authorization": "Bearer !!!not-base64!!!"}
    ).status_code == 401
    assert client.get(
        "/api/watchlists", headers={"Authorization": "Basic whatever"}
    ).status_code == 401


def test_dev_login_is_idempotent_and_creates_a_default_watchlist(client):
    first = client.post("/api/auth/dev-login", json={"email": "a@b.com"}).json()
    second = client.post("/api/auth/dev-login", json={"email": "a@b.com"}).json()

    assert first["user_id"] == second["user_id"]
    lists = client.get("/api/watchlists", headers=auth(client, "a@b.com")).json()
    assert len(lists) == 1


# --- Ownership --------------------------------------------------------------


def test_another_users_watchlist_is_404_not_403(client):
    owner = auth(client, "owner@example.com")
    watchlist_id = client.get("/api/watchlists", headers=owner).json()[0]["id"]
    intruder = auth(client, "intruder@example.com")

    assert client.get(f"/api/watchlists/{watchlist_id}", headers=intruder).status_code == 404
    assert client.delete(f"/api/watchlists/{watchlist_id}", headers=intruder).status_code == 404
    assert client.post(
        f"/api/watchlists/{watchlist_id}/items",
        headers=intruder,
        json={"symbol": "TCS"},
    ).status_code == 404


# --- Items and the hot set --------------------------------------------------


def test_adding_an_item_makes_the_symbol_hot_and_removing_it_cools(client):
    headers = auth(client)
    watchlist_id = client.get("/api/watchlists", headers=headers).json()[0]["id"]

    added = client.post(
        f"/api/watchlists/{watchlist_id}/items", headers=headers, json={"symbol": "TCS"}
    ).json()
    assert client.cache.hot_set() == ["TCS"]

    # Same _now_ist() default as created_at. The old datetime.utcnow default
    # stored UTC into a naive-IST schema, so this was out by 5h30m.
    drift = datetime.now(IST) - datetime.fromisoformat(added["added_at"])
    assert abs(drift.total_seconds()) < 10, f"added_at is {drift} from IST wall clock"

    client.delete(f"/api/watchlists/{watchlist_id}/items/TCS", headers=headers)
    assert client.cache.hot_set() == []


def test_deleting_a_watchlist_releases_every_symbol_it_held(client):
    """Otherwise the poller keeps fetching a whole list nobody watches."""
    headers = auth(client)
    watchlist_id = client.get("/api/watchlists", headers=headers).json()[0]["id"]
    for symbol in ("TCS", "INFY"):
        client.post(
            f"/api/watchlists/{watchlist_id}/items", headers=headers, json={"symbol": symbol}
        )
    assert len(client.cache.hot_set()) == 2

    client.delete(f"/api/watchlists/{watchlist_id}", headers=headers)

    assert client.cache.hot_set() == []


def test_duplicate_add_is_409_and_unknown_symbol_is_404(client):
    headers = auth(client)
    watchlist_id = client.get("/api/watchlists", headers=headers).json()[0]["id"]
    client.post(f"/api/watchlists/{watchlist_id}/items", headers=headers, json={"symbol": "TCS"})

    duplicate = client.post(
        f"/api/watchlists/{watchlist_id}/items", headers=headers, json={"symbol": "TCS"}
    )
    unknown = client.post(
        f"/api/watchlists/{watchlist_id}/items", headers=headers, json={"symbol": "NOSUCH"}
    )

    assert duplicate.status_code == 409
    assert unknown.status_code == 404


def test_patch_only_touches_the_fields_that_were_sent(client):
    """A PATCH setting a note must not silently erase a cost basis."""
    headers = auth(client)
    watchlist_id = client.get("/api/watchlists", headers=headers).json()[0]["id"]
    client.post(
        f"/api/watchlists/{watchlist_id}/items",
        headers=headers,
        json={"symbol": "TCS", "cost_basis": 3800.0},
    )

    updated = client.patch(
        f"/api/watchlists/{watchlist_id}/items/TCS", headers=headers, json={"note": "earnings"}
    ).json()

    assert updated["note"] == "earnings"
    assert updated["cost_basis"] == 3800.0


# --- Seen -------------------------------------------------------------------


def test_seen_rejects_unwatched_symbols_without_failing_the_batch(client):
    """Eleven good rows must not be punished for one bad one."""
    headers = auth(client)
    watchlist_id = client.get("/api/watchlists", headers=headers).json()[0]["id"]
    client.post(f"/api/watchlists/{watchlist_id}/items", headers=headers, json={"symbol": "TCS"})

    response = client.post(
        "/api/seen",
        headers=headers,
        json={
            "entries": [
                {"symbol": "TCS", "seen_at": "2026-09-04T14:00:00+05:30", "price": 3900},
                {"symbol": "HDFCBANK", "seen_at": "2026-09-04T14:00:00+05:30", "price": 1650},
            ]
        },
    ).json()

    assert response["updated"] == 1
    assert response["rejected"] == ["HDFCBANK"]


# --- Digest -----------------------------------------------------------------


def test_a_symbol_with_no_quote_never_reports_a_price(client):
    """price must be null, not the 0.0 placeholder the assembler carries."""
    headers = auth(client)
    watchlist_id = client.get("/api/watchlists", headers=headers).json()[0]["id"]
    client.post(f"/api/watchlists/{watchlist_id}/items", headers=headers, json={"symbol": "TCS"})

    digest = client.get("/api/digest", headers=headers).json()

    row = digest["degraded"][0]
    assert row["price"] is None
    assert row["freshness"] == "unavailable"
    assert digest["needs_attention"] == []


# --- SSE --------------------------------------------------------------------


class DisconnectingRequest:
    """The parts of Request the stream generator touches."""

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}

    async def is_disconnected(self) -> bool:
        return False


def test_the_stream_closes_its_subscription_when_the_client_disconnects(client):
    """The leak that is invisible in manual testing.

    A browser reconnects an SSE stream aggressively. If the subscription is
    not closed on disconnect, every reconnect leaves a dead subscriber behind
    and the list grows without bound while everything continues to look
    perfectly healthy.

    Exercised by closing the async generator, which is exactly what Starlette
    does when a client goes away -- not through TestClient, which buffers a
    stream and would never terminate an open one.
    """
    fresh = client.cache
    assert fresh._subscribers == []

    async def open_and_drop() -> None:
        stream = _stream(DisconnectingRequest(), ["TCS"], [])
        first = await stream.__anext__()
        assert first == "retry:3000\n\n"
        assert len(fresh._subscribers) == 1, "subscription was never registered"
        await stream.aclose()

    for _ in range(3):
        asyncio.run(open_and_drop())

    assert fresh._subscribers == [], "SSE subscription leaked on disconnect"


def test_the_stream_replays_only_what_the_client_missed(client):
    """Last-Event-ID resume, so a dropped connection does not lose events."""
    backlog = [
        {"type": "event", "id": 7, "data": {"symbol": "TCS", "explanation": "moved"}}
    ]

    async def collect() -> list[str]:
        stream = _stream(DisconnectingRequest(), ["TCS"], backlog)
        frames = [await stream.__anext__(), await stream.__anext__()]
        await stream.aclose()
        return frames

    frames = asyncio.run(collect())

    assert frames[0].startswith("id:7\nevent:event\ndata:")
    assert frames[1] == "retry:3000\n\n"
    assert client.cache._subscribers == []


def test_the_stream_only_subscribes_to_symbols_the_user_actually_watches(client):
    """The query string is client input and is not trusted."""
    headers = auth(client)
    watchlist_id = client.get("/api/watchlists", headers=headers).json()[0]["id"]
    client.post(f"/api/watchlists/{watchlist_id}/items", headers=headers, json={"symbol": "TCS"})

    watched = _watched_symbols(next(client.app.dependency_overrides[get_session]()), 1)

    assert "TCS" in watched
    assert "HDFCBANK" not in watched


# --- Health -----------------------------------------------------------------


def test_health_reports_the_poller_not_just_the_api(client):
    body = client.get("/health").json()

    assert body["status"] == "ok"
    assert body["provider"] == "replay"
    # None until a poll cycle runs: an API up in front of a dead worker must
    # be distinguishable from a healthy system.
    assert body["last_poll_at"] is None
