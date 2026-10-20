"""
Request dependencies — turning a token into the current user, and enforcing access.

Every protected endpoint uses `current_user`. This is the single chokepoint where the
server decides "who is this and what unit are they in" — so isolation is enforced here,
once, on the server, never trusted to the app.
"""
from fastapi import Depends, HTTPException, Header
from sqlalchemy.orm import Session

from .db import get_db
from .models import User, Unit
from .security import decode_token


def current_user(
    authorization: str = Header(default=""),
    db: Session = Depends(get_db),
) -> User:
    # Expect "Authorization: Bearer <token>"
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = authorization.split(" ", 1)[1].strip()
    user_id = decode_token(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    user = db.query(User).filter(User.id == user_id).first()
    if not user or not user.active:
        raise HTTPException(status_code=401, detail="Account not found or disabled")
    return user


def user_unit_ids(user: User) -> list[str]:
    """The unit ids this user BELONGS to — their home ward(s). (Usually one.)"""
    return [u.id for u in user.units]


def accessible_unit_ids(user: User, db: Session) -> list[str]:
    """The units this user may OPEN a patient in. An admin is institution-wide, so may open
    any unit's patient; everyone else is limited to their own unit(s). This is the access
    check; which ward's LIST you see is a separate choice (see active_unit)."""
    if user.role == "admin":
        return [u.id for u in db.query(Unit).all()]
    return user_unit_ids(user)


def active_unit(
    x_unit_id: str = Header(default="", alias="X-Unit-Id"),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> str | None:
    """The ward the client is currently viewing, from the optional `X-Unit-Id` header.
    Returned only if the user may access it; otherwise None, and callers fall back to the
    user's home unit. This is what lets an admin flip between wards without changing data
    isolation — a non-admin can only ever name a unit they already belong to."""
    uid = (x_unit_id or "").strip()
    if not uid:
        return None
    return uid if uid in accessible_unit_ids(user, db) else None


def require_admin(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return user
