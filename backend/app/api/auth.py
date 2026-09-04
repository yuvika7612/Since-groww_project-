"""Development authentication.

NOT SECURE. DEV ONLY. Replace with OAuth by swapping this one dependency.

The token is a base64 of "user_id:email". It is not signed, not encrypted and
not expiring: anyone can mint one for any user by base64-encoding a string.
That is a deliberate, stated trade for a 72-hour build, not an oversight.

The reason it is shaped this way rather than half-built as a password system:
every route depends on `get_current_user` and nothing else. Swapping in real
OAuth means rewriting this one function and touching no route. A half-real
auth system would spread assumptions about sessions and hashing across the
whole API and still not be safe.
"""

from __future__ import annotations

import base64
import binascii

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import User, Watchlist
from app.schemas import DevLoginRequest, TokenResponse

router = APIRouter(prefix="/auth", tags=["auth"])


def _encode_token(user_id: int, email: str) -> str:
    return base64.urlsafe_b64encode(f"{user_id}:{email}".encode()).decode()


def _decode_token(token: str) -> tuple[int, str]:
    raw = base64.urlsafe_b64decode(token.encode()).decode()
    user_id, _, email = raw.partition(":")
    return int(user_id), email


@router.post("/dev-login", response_model=TokenResponse)
def dev_login(
    payload: DevLoginRequest, session: Session = Depends(get_session)
) -> TokenResponse:
    """Create the user if absent and hand back a token.

    Also creates a default watchlist, because a user with no watchlist has no
    digest, and a first-run experience of "nothing here" is indistinguishable
    from the product being broken.
    """
    email = payload.email.strip().lower()
    if not email:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "email is required")

    user = session.scalars(select(User).where(User.email == email)).first()
    if user is None:
        user = User(email=email)
        session.add(user)
        session.flush()
        session.add(Watchlist(user_id=user.id, name="My watchlist"))
    session.commit()

    return TokenResponse(user_id=user.id, token=_encode_token(user.id, user.email))


def get_current_user(
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> User:
    """Resolve the bearer token to a user.

    Always 401, never 403. A missing or malformed token is a failure to
    identify the caller at all, not a caller who has been identified and
    denied. Returning 403 here would also tell an anonymous prober that the
    resource exists.
    """
    unauthorised = HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "Missing or invalid bearer token",
        headers={"WWW-Authenticate": "Bearer"},
    )

    if not authorization or not authorization.lower().startswith("bearer "):
        raise unauthorised

    try:
        user_id, email = _decode_token(authorization.split(" ", 1)[1].strip())
    except (ValueError, UnicodeDecodeError, binascii.Error):
        raise unauthorised from None

    user = session.get(User, user_id)
    if user is None or user.email != email:
        raise unauthorised
    return user


def get_current_user_allowing_beacon(
    authorization: str | None = Header(default=None),
    token: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> User:
    """get_current_user, plus a query-string token for header-less clients.

    Two browser APIs cannot set request headers at all, and both are load
    bearing here: navigator.sendBeacon, which carries the page-hide flush of
    the read watermark (the moment a user closes the app is exactly when
    "what have I already read" is being recorded), and EventSource, which
    carries the live stream.

    Deliberately scoped to the single endpoint that needs it rather than
    widened across the API: a token in a query string ends up in access logs
    and browser history. That is an acceptable concession for an unsigned
    dev-login token and would not be for a real one, which is another reason
    the auth swap is one function.
    """
    if authorization:
        return get_current_user(authorization=authorization, session=session)
    if token:
        return get_current_user(authorization=f"Bearer {token}", session=session)
    raise HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "Missing or invalid bearer token",
        headers={"WWW-Authenticate": "Bearer"},
    )
