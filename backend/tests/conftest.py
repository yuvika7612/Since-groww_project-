"""Database fixtures.

An in-memory SQLite database per test, built from the real models rather than
a hand-written schema, so a model change that breaks the assembler breaks
these tests too rather than silently diverging from them.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, Symbol, User, Watchlist, WatchlistItem


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        yield db


def make_user(session: Session, email: str = "demo@example.com") -> User:
    user = User(email=email, created_at=datetime(2026, 9, 1))
    session.add(user)
    session.flush()
    return user


def make_symbols(session: Session, *symbols: str, benchmark: str = "^NSEI") -> None:
    """Register instruments, including the benchmark itself.

    The index is a row in `symbols` like anything else: it is polled, quoted
    and cached the same way, it is simply not usually watched.
    """
    for symbol in {*symbols, benchmark}:
        session.add(
            Symbol(
                symbol=symbol,
                name=f"{symbol} Ltd" if not symbol.startswith("^") else symbol,
                exchange="NSE",
                benchmark=benchmark,
            )
        )
    session.flush()


def make_watchlist(
    session: Session,
    user: User,
    symbols: list[str],
    cost_basis: dict[str, float] | None = None,
) -> Watchlist:
    watchlist = Watchlist(user_id=user.id, name="My watchlist")
    session.add(watchlist)
    session.flush()
    for symbol in symbols:
        session.add(
            WatchlistItem(
                watchlist_id=watchlist.id,
                symbol=symbol,
                added_at=datetime(2026, 9, 1),
                cost_basis=(cost_basis or {}).get(symbol),
            )
        )
    session.flush()
    return watchlist
