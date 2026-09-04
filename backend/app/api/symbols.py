"""Symbol search and detail."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.db import get_session
from app.models import ChangeEventRow, Symbol, SymbolStats, User
from app.schemas import EventOut, SymbolDetail, SymbolOut, SymbolStatsOut, ist

router = APIRouter(prefix="/symbols", tags=["symbols"])

# Capped server-side. A client-supplied limit is an untrusted input, and
# "limit=100000" is one request that reads the whole instrument table.
MAX_SEARCH_RESULTS = 50
RECENT_EVENT_COUNT = 5


@router.get("/search", response_model=list[SymbolOut])
def search_symbols(
    q: str = Query(default="", max_length=64),
    limit: int = Query(default=10, ge=1),
    _: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> list[SymbolOut]:
    query = q.strip()
    if not query:
        return []

    pattern = f"%{query}%"
    rows = session.scalars(
        select(Symbol)
        .where(or_(Symbol.symbol.ilike(pattern), Symbol.name.ilike(pattern)))
        .order_by(Symbol.symbol)
        .limit(min(limit, MAX_SEARCH_RESULTS))
    ).all()
    return [
        SymbolOut(symbol=row.symbol, name=row.name, exchange=row.exchange)
        for row in rows
    ]


@router.get("/{symbol}", response_model=SymbolDetail)
def get_symbol(
    symbol: str,
    _: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> SymbolDetail:
    symbol = symbol.strip().upper()
    instrument = session.get(Symbol, symbol)
    if instrument is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown symbol {symbol}")

    stats = session.get(SymbolStats, symbol)
    events = session.scalars(
        select(ChangeEventRow)
        .where(ChangeEventRow.symbol == symbol)
        .order_by(ChangeEventRow.occurred_at.desc())
        .limit(RECENT_EVENT_COUNT)
    ).all()

    return SymbolDetail(
        symbol=instrument.symbol,
        name=instrument.name,
        exchange=instrument.exchange,
        sector=instrument.sector,
        benchmark=instrument.benchmark,
        stats=(
            SymbolStatsOut(
                mean_ret_30d=stats.mean_ret_30d,
                std_ret_30d=stats.std_ret_30d,
                avg_vol_20d=stats.avg_vol_20d,
                high_52w=stats.high_52w,
                low_52w=stats.low_52w,
                beta_60d=stats.beta_60d,
                sample_size=stats.sample_size,
                computed_at=ist(stats.computed_at),
                # One standard deviation of this symbol's own daily return.
                # Everything the product claims about "meaningful" is
                # measured against this number, so it is worth showing.
                expected_daily_move=stats.std_ret_30d,
            )
            if stats
            else None
        ),
        recent_events=[
            EventOut(
                type=event.event_type,
                severity=event.severity,
                explanation=event.explanation,
                payload=event.payload or {},
                occurred_at=ist(event.occurred_at),
            )
            for event in events
        ],
    )
