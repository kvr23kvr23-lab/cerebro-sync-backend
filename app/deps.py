"""
Request dependencies — turning a token into the current user, and enforcing access.

Every protected endpoint uses `current_user`. This is the single chokepoint where the
server decides "who is this and what unit are they in" — so isolation is enforced here,
once, on the server, never trusted to the app.
"""
from fastapi import Depends, HTTPException, Header
from sqlalchemy.orm import Session

from .db import get_db
from .models import User
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
    """The unit ids this user may access. (Stage 1: usually one.)"""
    return [u.id for u in user.units]


def require_admin(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return user
