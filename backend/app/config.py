"""Application configuration.

Defaults are chosen so `uvicorn app.main:app` works on a bare checkout with no
Postgres and no Redis running. Docker Compose overrides them via environment.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # SQLite by default so the project runs with zero infrastructure.
    # docker-compose sets DATABASE_URL to Postgres.
    database_url: str = "sqlite:///./data/watchlist.db"

    # Empty means "use the in-process cache shim" (see app/cache.py).
    redis_url: str = ""

    # "replay" plays a recorded session against a virtual clock.
    # "yfinance" hits the live provider.
    market_provider: str = "replay"
    replay_fixture: str = "./data/session.jsonl"
    # At 10x speed a 5s poll interval = 50 virtual seconds < 90s stale threshold.
    # At 60x (old default) polls were 300 virtual seconds apart and every row degraded.
    replay_speed: float = 10.0

    # Poll cadence in seconds, by market state.
    poll_interval_open: int = 5
    poll_interval_preopen: int = 30
    poll_interval_closed: int = 600

    # --- Detection thresholds -------------------------------------------
    # A move must clear this many standard deviations of the symbol's own
    # 30-day return distribution before it is considered a signal at all.
    z_threshold: float = 1.5
    # Relative volume above this confirms a price move as participation-backed.
    rvol_threshold: float = 1.8
    # Overnight gap, as a fraction, that counts as a news event.
    gap_threshold: float = 0.02
    # Residual return (stock minus beta x index) that counts as stock-specific.
    residual_threshold: float = 0.015

    # How many rows the digest is allowed to surface, regardless of how
    # violent the session was. See digest/service.py for the reasoning.
    attention_budget: int = 5

    # A quote older than this is rendered as stale rather than as current.
    stale_after_seconds: int = 90

    # A tick further than this many sigma from the last accepted price is
    # quarantined unless a second source corroborates it.
    tick_sanity_sigma: float = 12.0

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
