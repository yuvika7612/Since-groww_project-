"""Liveness counters for the poll loop.

Module-level and deliberately unguarded. There is one process and one poller
writing these, and the health endpoint reading a value one cycle out of date
is not a problem worth a lock.

last_poll_at is the field that matters: it is how you tell at a glance that
the worker has died. A healthy API in front of a dead poller serves prices
that quietly stop moving, which looks fine and is the worst failure this
system has.
"""

from __future__ import annotations

from datetime import datetime

last_poll_at: datetime | None = None
last_poll_symbol_count: int = 0
last_poll_rejected_count: int = 0
