"""
Admin endpoints — institution administration: units (wards) and users.

A user with role "admin" is an INSTITUTION admin: they can create and rename units, create
users in any unit, reset passwords, enable/disable accounts, and move a user to a different
unit. Patient DATA stays isolated per unit — this is only about accounts and unit setup.
Grant admin carefully: an admin can manage every unit.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .db import get_db
from .models import User, Unit
from .deps import require_admin
from .security import hash_password
from .schemas import CreateUserIn, SetActiveIn, ResetPasswordIn, CreateUnitIn

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _user_out(u: User) -> dict:
    return {
        "id": u.id,
        "username": u.username,
        "display_name": u.display_name or "",
        "role": u.role,
        "active": bool(u.active),
        "units": [{"id": x.id, "name": x.name} for x in u.units],
    }


# ------------------------------------------------------------------------- units/wards
@router.get("/units")
def list_units(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    return [
        {"id": u.id, "name": u.name, "user_count": len(u.users)}
        for u in db.query(Unit).order_by(Unit.name).all()
    ]


@router.post("/units")
def create_unit(body: CreateUnitIn, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="A name is required.")
    if db.query(Unit).filter(Unit.name == name).first():
        raise HTTPException(status_code=409, detail="A unit with that name already exists.")
    u = Unit(name=name)
    db.add(u); db.commit(); db.refresh(u)
    return {"id": u.id, "name": u.name, "user_count": 0}


class RenameIn(BaseModel):
    name: str


@router.post("/units/{uid}/rename")
def rename_unit(uid: str, body: RenameIn, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    unit = db.query(Unit).filter(Unit.id == uid).first()
    if not unit:
        raise HTTPException(status_code=404, detail="Unit not found.")
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="A name is required.")
    if db.query(Unit).filter(Unit.name == name, Unit.id != uid).first():
        raise HTTPException(status_code=409, detail="Another unit already has that name.")
    unit.name = name
    db.commit()
    return {"id": unit.id, "name": unit.name}


# ------------------------------------------------------------------------------- users
@router.get("/users")
def list_users(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    users = db.query(User).all()
    users.sort(key=lambda x: (x.role != "admin", (x.units[0].name if x.units else ""), x.username))
    return [_user_out(u) for u in users]


def _resolve_unit(db: Session, unit_ids, admin: User) -> Unit:
    if unit_ids:
        unit = db.query(Unit).filter(Unit.id == unit_ids[0]).first()
        if not unit:
            raise HTTPException(status_code=400, detail="That unit doesn't exist.")
        return unit
    if admin.units:
        return admin.units[0]
    raise HTTPException(status_code=400, detail="Pick a unit for this user.")


@router.post("/users")
def create_user(body: CreateUserIn, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    uname = (body.username or "").strip().lower()
    if not uname or not (body.password or "").strip():
        raise HTTPException(status_code=400, detail="Username and password are required.")
    if " " in uname:
        raise HTTPException(status_code=400, detail="Username can't contain spaces.")
    if db.query(User).filter(User.username == uname).first():
        raise HTTPException(status_code=409, detail="That username is already taken.")
    unit = _resolve_unit(db, body.unit_ids, admin)
    role = body.role if body.role in ("user", "admin") else "user"
    u = User(
        username=uname, password_hash=hash_password(body.password),
        display_name=(body.display_name or "").strip(), role=role, active=True,
    )
    u.units = [unit]
    db.add(u); db.commit(); db.refresh(u)
    return _user_out(u)


def _target(db: Session, uid: str) -> User:
    u = db.query(User).filter(User.id == uid).first()
    if not u:
        raise HTTPException(status_code=404, detail="User not found.")
    return u


@router.post("/users/{uid}/password")
def reset_password(uid: str, body: ResetPasswordIn, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    u = _target(db, uid)
    if not (body.password or "").strip():
        raise HTTPException(status_code=400, detail="New password is required.")
    u.password_hash = hash_password(body.password)
    db.commit()
    return {"ok": True}


@router.post("/users/{uid}/active")
def set_active(uid: str, body: SetActiveIn, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    u = _target(db, uid)
    if u.id == admin.id and not body.active:
        raise HTTPException(status_code=400, detail="You can't disable your own admin account.")
    u.active = bool(body.active)
    db.commit()
    return {"ok": True}


class SetUnitIn(BaseModel):
    unit_id: str


@router.post("/users/{uid}/unit")
def set_user_unit(uid: str, body: SetUnitIn, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    u = _target(db, uid)
    unit = db.query(Unit).filter(Unit.id == body.unit_id).first()
    if not unit:
        raise HTTPException(status_code=400, detail="That unit doesn't exist.")
    u.units = [unit]
    db.commit()
    return _user_out(u)
