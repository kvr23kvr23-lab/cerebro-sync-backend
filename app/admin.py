"""
Admin endpoints — team (user) management, powering the in-app Team screen.

Every route requires an admin (require_admin). An admin manages only their OWN unit's
users: new users are created in the admin's unit, and password resets / enable-disable are
allowed only on users who belong to that unit. This preserves the per-unit isolation the
rest of the API already enforces — an admin can never see or touch another unit's team.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .db import get_db
from .models import User, Unit
from .deps import require_admin
from .security import hash_password
from .schemas import CreateUserIn, SetActiveIn, ResetPasswordIn

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _admin_unit(admin: User) -> Unit:
    if not admin.units:
        raise HTTPException(status_code=403, detail="Your admin account is not assigned to a unit.")
    return admin.units[0]


def _target_in_unit(db: Session, admin: User, uid: str) -> User:
    unit = _admin_unit(admin)
    u = db.query(User).filter(User.id == uid).first()
    if not u or unit.id not in [x.id for x in u.units]:
        raise HTTPException(status_code=404, detail="User not found in your unit.")
    return u


def _out(u: User) -> dict:
    return {
        "id": u.id, "username": u.username, "display_name": u.display_name or "",
        "role": u.role, "active": bool(u.active),
    }


@router.get("/users")
def list_users(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Everyone in the admin's unit (admins first, then by username)."""
    unit = _admin_unit(admin)
    users = sorted(unit.users, key=lambda x: (x.role != "admin", x.username))
    return [_out(u) for u in users]


@router.post("/users")
def create_user(body: CreateUserIn, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Create a user in the admin's own unit. `unit_ids` from the client is ignored on
    purpose — a user is always placed in the admin's unit, never an arbitrary one."""
    unit = _admin_unit(admin)
    uname = (body.username or "").strip().lower()
    if not uname or not (body.password or "").strip():
        raise HTTPException(status_code=400, detail="Username and password are required.")
    if " " in uname:
        raise HTTPException(status_code=400, detail="Username can't contain spaces.")
    if db.query(User).filter(User.username == uname).first():
        raise HTTPException(status_code=409, detail="That username is already taken.")
    role = body.role if body.role in ("user", "admin") else "user"
    u = User(
        username=uname, password_hash=hash_password(body.password),
        display_name=(body.display_name or "").strip(), role=role, active=True,
    )
    u.units = [unit]
    db.add(u); db.commit(); db.refresh(u)
    return _out(u)


@router.post("/users/{uid}/password")
def reset_password(uid: str, body: ResetPasswordIn, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    u = _target_in_unit(db, admin, uid)
    if not (body.password or "").strip():
        raise HTTPException(status_code=400, detail="New password is required.")
    u.password_hash = hash_password(body.password)
    db.commit()
    return {"ok": True}


@router.post("/users/{uid}/active")
def set_active(uid: str, body: SetActiveIn, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    u = _target_in_unit(db, admin, uid)
    if u.id == admin.id and not body.active:
        raise HTTPException(status_code=400, detail="You can't disable your own admin account.")
    u.active = bool(body.active)
    db.commit()
    return {"ok": True}
