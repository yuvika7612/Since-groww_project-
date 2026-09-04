"""Provider selection.

The instance is module-level and built exactly once. This is not a
micro-optimisation: ReplayProvider holds the virtual clock, so constructing a
second one silently rewinds the demo to its start. A provider built inside a
request handler or a FastAPI dependency would do that on every request.
"""

from __future__ import annotations

from app.config import settings
from app.providers.base import MarketDataProvider
from app.providers.replay import ReplayProvider


def build_provider() -> MarketDataProvider:
    if settings.market_provider == "replay":
        return ReplayProvider(settings.replay_fixture, settings.replay_speed)

    # Imported inside the branch because importing yfinance costs about two
    # and a half seconds and drags in the whole live-data stack. The replay
    # demo must not pay that, and must not fail to start if the live provider
    # cannot be imported at all -- see the reasoning in providers/base.py.
    from app.providers.yfinance_provider import YFinanceProvider

    return YFinanceProvider()


provider: MarketDataProvider = build_provider()
