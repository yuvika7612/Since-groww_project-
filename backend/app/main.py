"""Application entry point.

    uvicorn app.main:app --reload

The poller is not started here. Phase 5 owns it, and the API is deliberately
able to serve without it: reads come from the cache and the database, so a
dead worker degrades into stale prices that are *labelled* stale rather than
into a broken API. /health is how you tell the difference.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import state
from app.api import auth, debug, digest, symbols, watchlists
from app.cache import cache
from app.config import settings
from app.db import init_db
from app.market.calendar import market_state
from app.providers.factory import provider
from app.schemas import HealthOut, ist

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    log.info(
        "started: provider=%s database=%s", provider.name, settings.database_url
    )
    yield


app = FastAPI(
    title="Smart Market Watchlist",
    description="What has meaningfully changed since you last checked.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # The browser cannot read Last-Event-ID handling or custom headers on a
    # cross-origin response unless they are exposed.
    expose_headers=["*"],
)

app.include_router(auth.router, prefix="/api")
app.include_router(watchlists.router, prefix="/api")
app.include_router(symbols.router, prefix="/api")
app.include_router(digest.router, prefix="/api")
app.include_router(debug.router, prefix="/api")


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
    """One consistent error shape, and the traceback in the log, not the body."""
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"error": str(exc), "type": type(exc).__name__},
    )


@app.get("/health", response_model=HealthOut)
def health() -> HealthOut:
    """Whether the API is up, and separately whether the poller still is.

    last_poll_at is the field that matters. An API answering happily in front
    of a dead worker serves prices that have quietly stopped moving, and
    nothing else in the system will tell you.
    """
    return HealthOut(
        status="ok",
        market_state=market_state(provider.now()).value,
        provider=provider.name,
        hot_set_size=len(cache.hot_set()),
        last_poll_at=ist(state.last_poll_at),
        last_poll_symbol_count=state.last_poll_symbol_count,
        last_poll_rejected_count=state.last_poll_rejected_count,
    )
