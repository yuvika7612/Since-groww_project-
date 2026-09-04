"""Watchlist CRUD.

Two rules run through every route here.

Ownership is checked on every single one, and a watchlist belonging to
somebody else returns 404 rather than 403. A 403 confirms the id exists,
which hands an attacker a working enumeration oracle for free.

Membership changes drive the hot set. Adding an item is what puts a symbol
into the poll loop; removing the last reference is what takes it out. Forget
the release and the poller keeps fetching a symbol nobody watches, forever,
and the leak is invisible because nothing breaks.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.cache import cache
from app.db import get_session
from app.models import Symbol, User, Watchlist, WatchlistItem
from app.schemas import (
    AddItemRequest,
    CreateWatchlistRequest,
    UpdateItemRequest,
    WatchlistDetail,
    WatchlistItemOut,
    WatchlistSummary,
    ist,
)

router = APIRouter(prefix="/watchlists", tags=["watchlists"])


def _owned_or_404(session: Session, user: User, watchlist_id: int) -> Watchlist:
    watchlist = session.scalars(
        select(Watchlist).where(
            Watchlist.id == watchlist_id, Watchlist.user_id == user.id
        )
    ).first()
    if watchlist is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Watchlist not found")
    return watchlist


@router.get("", response_model=list[WatchlistSummary])
def list_watchlists(
    user: User = Depends(get_current_user), session: Session = Depends(get_session)
) -> list[WatchlistSummary]:
    # One grouped query rather than a count per watchlist.
    rows = session.execute(
        select(Watchlist.id, Watchlist.name, func.count(WatchlistItem.id))
        .outerjoin(WatchlistItem, WatchlistItem.watchlist_id == Watchlist.id)
        .where(Watchlist.user_id == user.id)
        .group_by(Watchlist.id, Watchlist.name)
        .order_by(Watchlist.id)
    ).all()
    return [
        WatchlistSummary(id=row[0], name=row[1], item_count=row[2]) for row in rows
    ]


@router.post("", response_model=WatchlistSummary, status_code=status.HTTP_201_CREATED)
def create_watchlist(
    payload: CreateWatchlistRequest,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> WatchlistSummary:
    watchlist = Watchlist(user_id=user.id, name=payload.name)
    session.add(watchlist)
    session.commit()
    return WatchlistSummary(id=watchlist.id, name=watchlist.name, item_count=0)


@router.get("/{watchlist_id}", response_model=WatchlistDetail)
def get_watchlist(
    watchlist_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> WatchlistDetail:
    watchlist = _owned_or_404(session, user, watchlist_id)

    rows = session.execute(
        select(
            WatchlistItem.symbol,
            WatchlistItem.cost_basis,
            WatchlistItem.note,
            WatchlistItem.price_at_add,
            WatchlistItem.added_at,
            Symbol.name,
        )
        .join(Symbol, Symbol.symbol == WatchlistItem.symbol)
        .where(WatchlistItem.watchlist_id == watchlist_id)
        .order_by(WatchlistItem.symbol)
    ).all()

    # One batched cache read for the whole list, not one per row.
    quotes = cache.get_quotes([row.symbol for row in rows])

    items = []
    for row in rows:
        quote = quotes.get(row.symbol)
        items.append(
            WatchlistItemOut(
                symbol=row.symbol,
                name=row.name,
                cost_basis=row.cost_basis,
                note=row.note,
                price_at_add=row.price_at_add,
                added_at=ist(row.added_at),
                price=quote.price if quote else None,
                as_of=ist(quote.as_of) if quote else None,
                source=quote.source if quote else None,
                freshness=quote.freshness.value if quote else None,
            )
        )
    return WatchlistDetail(id=watchlist.id, name=watchlist.name, items=items)


@router.delete(
    "/{watchlist_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    response_model=None,
)
def delete_watchlist(
    watchlist_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    watchlist = _owned_or_404(session, user, watchlist_id)

    # Every item in it was holding a reference. Deleting the watchlist has to
    # release all of them or the poll loop keeps every one of those symbols
    # hot with nobody watching.
    symbols = session.scalars(
        select(WatchlistItem.symbol).where(WatchlistItem.watchlist_id == watchlist_id)
    ).all()

    session.delete(watchlist)
    session.commit()

    for symbol in symbols:
        cache.remove_from_hot_set(symbol)


@router.post(
    "/{watchlist_id}/items",
    response_model=WatchlistItemOut,
    status_code=status.HTTP_201_CREATED,
)
def add_item(
    watchlist_id: int,
    payload: AddItemRequest,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> WatchlistItemOut:
    _owned_or_404(session, user, watchlist_id)
    symbol = payload.symbol.strip().upper()

    instrument = session.get(Symbol, symbol)
    if instrument is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown symbol {symbol}")

    existing = session.scalars(
        select(WatchlistItem).where(
            WatchlistItem.watchlist_id == watchlist_id, WatchlistItem.symbol == symbol
        )
    ).first()
    if existing is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"{symbol} is already in this watchlist"
        )

    # "How has it done since I started caring about it" is a different and
    # often better question than "how has it done today", and it can only be
    # answered if the price is captured at this moment.
    quote = cache.get_quote(symbol)

    item = WatchlistItem(
        watchlist_id=watchlist_id,
        symbol=symbol,
        cost_basis=payload.cost_basis,
        note=payload.note,
        price_at_add=quote.price if quote else None,
    )
    session.add(item)
    session.commit()

    cache.add_to_hot_set(symbol)

    return WatchlistItemOut(
        symbol=symbol,
        name=instrument.name,
        cost_basis=item.cost_basis,
        note=item.note,
        price_at_add=item.price_at_add,
        added_at=ist(item.added_at),
        price=quote.price if quote else None,
        as_of=ist(quote.as_of) if quote else None,
        source=quote.source if quote else None,
        freshness=quote.freshness.value if quote else None,
    )


@router.patch("/{watchlist_id}/items/{symbol}", response_model=WatchlistItemOut)
def update_item(
    watchlist_id: int,
    symbol: str,
    payload: UpdateItemRequest,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> WatchlistItemOut:
    _owned_or_404(session, user, watchlist_id)
    symbol = symbol.strip().upper()

    item = session.scalars(
        select(WatchlistItem).where(
            WatchlistItem.watchlist_id == watchlist_id, WatchlistItem.symbol == symbol
        )
    ).first()
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{symbol} is not in this watchlist")

    # Only fields actually present in the request body are touched, so a
    # PATCH that sets a note cannot silently erase a cost basis.
    provided = payload.model_dump(exclude_unset=True)
    if "cost_basis" in provided:
        item.cost_basis = provided["cost_basis"]
    if "note" in provided:
        item.note = provided["note"]
    session.commit()

    instrument = session.get(Symbol, symbol)
    quote = cache.get_quote(symbol)
    return WatchlistItemOut(
        symbol=symbol,
        name=instrument.name if instrument else symbol,
        cost_basis=item.cost_basis,
        note=item.note,
        price_at_add=item.price_at_add,
        added_at=ist(item.added_at),
        price=quote.price if quote else None,
        as_of=ist(quote.as_of) if quote else None,
        source=quote.source if quote else None,
        freshness=quote.freshness.value if quote else None,
    )


@router.delete(
    "/{watchlist_id}/items/{symbol}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    response_model=None,
)
def remove_item(
    watchlist_id: int,
    symbol: str,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    _owned_or_404(session, user, watchlist_id)
    symbol = symbol.strip().upper()

    item = session.scalars(
        select(WatchlistItem).where(
            WatchlistItem.watchlist_id == watchlist_id, WatchlistItem.symbol == symbol
        )
    ).first()
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{symbol} is not in this watchlist")

    session.delete(item)
    session.commit()

    # Release only after the row is gone. Releasing first would leave a
    # window where the symbol is unwatched in the cache but still watched in
    # the database, and a crash in between loses the reference permanently.
    cache.remove_from_hot_set(symbol)
