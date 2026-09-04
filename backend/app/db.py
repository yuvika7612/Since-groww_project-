"""Engine and session lifecycle.

One engine for the whole process. The poller and the request handlers share
it, which is what the SQLite settings below are for: the default driver
refuses to hand a connection to a thread other than the one that opened it,
and rollback journal mode makes a reader block on a writer. A watchlist API
serving reads while the poller writes change_events would deadlock on the
default settings within a session.

Sessions are short by design. Nothing holds one open across a poll interval,
because that is a transaction held open across a poll interval.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

# Importing this module is what registers every mapper on Base.metadata, so
# init_db() below sees the full schema. Any future model must live in
# app/models.py (or be imported by it) or create_all will silently skip it.
from app.models import Base

SQLITE_PREFIX = "sqlite:///"
_IS_SQLITE = settings.database_url.startswith("sqlite")


def _engine_kwargs() -> dict:
    if _IS_SQLITE:
        path = Path(settings.database_url[len(SQLITE_PREFIX):])
        if str(path.parent) not in ("", "."):
            path.parent.mkdir(parents=True, exist_ok=True)
        return {"connect_args": {"check_same_thread": False}}
    # A pooled Postgres connection idle across a database restart is dead but
    # still looks open. pre_ping turns that into a reconnect rather than a 500
    # on the first request after a deploy.
    return {"pool_pre_ping": True}


engine = create_engine(settings.database_url, future=True, **_engine_kwargs())

# expire_on_commit=False because rows are read, committed, then serialised.
# With the default, serialisation would re-query every attribute after commit.
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


if _IS_SQLITE:

    @event.listens_for(engine, "connect")
    def _configure_sqlite(dbapi_connection, connection_record) -> None:
        """WAL, so the API can read while the poller writes.

        In the default rollback journal, a write transaction locks the whole
        database and every concurrent reader blocks until it commits. WAL puts
        writes in a side log, so readers see the last committed snapshot and
        never wait. Without this the digest endpoint stalls behind the poll
        cycle.

        busy_timeout covers the case WAL does not: two *writers* still
        serialise, and the poller and a watchlist edit can collide. Five
        seconds of waiting beats an immediate "database is locked".
        """
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


def init_db() -> None:
    """Create any missing tables.

    Deliberately not a migration tool. The schema is young enough that
    create_all is honest about what it does; adding Alembic before there is a
    deployed database to migrate would be ceremony.
    """
    Base.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    """FastAPI dependency. Yields a session and always closes it."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
