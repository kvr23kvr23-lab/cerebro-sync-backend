"""
Cerebro multi-user backend — the shared data server.

One institution per deployment (its own server + database). Within it: units, users,
and unit-scoped patients & clinical records. This file wires the endpoints:
  auth (login / me / logout), patients, clinical records, and sync.

Every data endpoint is scoped to the caller's unit on the server — a user can never
receive another unit's data, even by asking.
"""
import datetime as dt
from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from .db import Base, engine, get_db
from . import models
from .models import Unit, User, Patient, ClinicalRecord
from .deps import current_user, user_unit_ids, require_admin, active_unit, accessible_unit_ids
from .security import hash_password, verify_password, create_token
from .schemas import (
    LoginIn, LoginOut, UserOut, UnitOut, PatientIn, RecordIn, ItemOut, SyncOut,
    CreateUnitIn, CreateUserIn, SetActiveIn, ResetPasswordIn,
)

# Create tables on startup (for Postgres & SQLite alike). For real schema changes later
# we'd use migrations; for Stage 1 this is enough.
Base.metadata.create_all(bind=engine)

# On a fresh cloud deployment, auto-create the first unit + login users so you can sign in
# immediately (no shell access needed). Controlled by env var; idempotent. Change the
# seeded passwords before any real use.
import os as _os
if _os.environ.get("SEED_ON_START") == "1":
    try:
        from .seed_data import run_seed
        run_seed()
    except Exception as _e:  # never let seeding crash startup
        print("Seed-on-start skipped:", _e)

app = FastAPI(title="Cerebro Multi-User Backend", version="0.1.0")

# AI endpoints (Draft with AI, note auto-extract) — same server, needs ANTHROPIC_API_KEY.
from .ai import router as ai_router
app.include_router(ai_router)

# Admin endpoints (Team screen — create users, reset passwords, enable/disable). Admin-only.
from .admin import router as admin_router
app.include_router(admin_router)

# Web Push endpoints (notifications that reach a phone even when the app is closed).
from .push import router as push_router
app.include_router(push_router)

# CORS: the frontend (Vercel) calls this API from the browser. "*" is fine for testing;
# tighten to the exact frontend URL for a real deployment.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _iso(d: dt.datetime) -> str:
    # Keep full microsecond precision. Incremental sync uses these timestamps as the
    # ">" cutoff for "what changed since last time" — rounding to whole seconds would
    # re-send every row that changed within the same second on the next sync.
    return (d or dt.datetime.utcnow()).isoformat() + "Z"


def _user_out(u: User) -> UserOut:
    return UserOut(
        id=u.id, username=u.username, display_name=u.display_name or "", role=u.role,
        units=[UnitOut(id=x.id, name=x.name) for x in u.units],
    )


def _patient_out(p: Patient) -> ItemOut:
    return ItemOut(
        id=p.id, unit_id=p.unit_id, data=p.data or {}, deleted=p.deleted,
        updated_at=_iso(p.updated_at), created_by=p.created_by, updated_by=p.updated_by,
    )


def _record_out(r: ClinicalRecord) -> ItemOut:
    return ItemOut(
        id=r.id, unit_id=r.unit_id, data=r.data or {}, kind=r.kind, patient_id=r.patient_id,
        deleted=r.deleted, updated_at=_iso(r.updated_at),
        created_by=r.created_by, updated_by=r.updated_by,
    )


# ----------------------------------------------------------------------------- health
@app.get("/api/health")
def health():
    return {"ok": True, "service": "cerebro-multiuser", "time": _iso(dt.datetime.utcnow())}


# ------------------------------------------------------------------------------- auth
@app.post("/api/auth/login", response_model=LoginOut)
def login(body: LoginIn, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == body.username.strip().lower()).first()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Wrong username or password")
    if not user.active:
        raise HTTPException(status_code=403, detail="This account is disabled")
    token = create_token(user.id)
    return LoginOut(token=token, user=_user_out(user))


@app.get("/api/auth/me", response_model=UserOut)
def me(user: User = Depends(current_user)):
    return _user_out(user)


@app.post("/api/auth/logout")
def logout(user: User = Depends(current_user)):
    # Stateless tokens: logout is handled client-side (discard the token). Endpoint exists
    # so the app has something to call; a server-side blocklist can be added later.
    return {"ok": True}


# --------------------------------------------------------------------------- patients
def _unit_guard(user: User) -> str:
    units = user_unit_ids(user)
    if not units:
        raise HTTPException(status_code=403, detail="You are not assigned to a unit")
    return units[0]  # Stage 1: one unit per user


@app.get("/api/patients", response_model=list[ItemOut])
def list_patients(user: User = Depends(current_user), active: str | None = Depends(active_unit), db: Session = Depends(get_db)):
    # Which ward's list: the one being viewed (active), else the user's home unit(s).
    units = [active] if active else user_unit_ids(user)
    rows = (
        db.query(Patient)
        .filter(Patient.unit_id.in_(units), Patient.deleted == False)  # noqa: E712
        .order_by(Patient.updated_at.desc())
        .all()
    )
    return [_patient_out(p) for p in rows]


@app.post("/api/patients", response_model=ItemOut)
def upsert_patient(body: PatientIn, user: User = Depends(current_user), active: str | None = Depends(active_unit), db: Session = Depends(get_db)):
    # A new patient lands in the ward currently being viewed (active), else the home unit.
    unit_id = active or _unit_guard(user)
    p = db.query(Patient).filter(Patient.id == body.id).first() if body.id else None
    if p:
        # Must be a unit the user may access (admins: any; others: their own).
        if p.unit_id not in accessible_unit_ids(user, db):
            raise HTTPException(status_code=403, detail="Not your unit's patient")
        p.data = body.data
        p.updated_by = user.id
    else:
        p = Patient(
            id=body.id or None, unit_id=unit_id, data=body.data,
            created_by=user.id, updated_by=user.id,
        )
        db.add(p)
    db.commit(); db.refresh(p)
    return _patient_out(p)


@app.get("/api/patients/{pid}", response_model=ItemOut)
def get_patient(pid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = db.query(Patient).filter(Patient.id == pid).first()
    if not p or p.unit_id not in accessible_unit_ids(user, db):
        raise HTTPException(status_code=404, detail="Not found")
    return _patient_out(p)


@app.delete("/api/patients/{pid}")
def delete_patient(pid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = db.query(Patient).filter(Patient.id == pid).first()
    if not p or p.unit_id not in accessible_unit_ids(user, db):
        raise HTTPException(status_code=404, detail="Not found")
    p.deleted = True
    p.updated_by = user.id
    db.commit()
    return {"ok": True}


# -------------------------------------------------------------------- clinical records
@app.post("/api/records", response_model=ItemOut)
def upsert_record(body: RecordIn, user: User = Depends(current_user), active: str | None = Depends(active_unit), db: Session = Depends(get_db)):
    # The patient must be in a unit the user may access.
    patient = db.query(Patient).filter(Patient.id == body.patient_id).first()
    if not patient or patient.unit_id not in accessible_unit_ids(user, db):
        raise HTTPException(status_code=403, detail="Not your unit's patient")
    r = db.query(ClinicalRecord).filter(ClinicalRecord.id == body.id).first() if body.id else None
    if r:
        if r.unit_id not in accessible_unit_ids(user, db):
            raise HTTPException(status_code=403, detail="Not your unit's record")
        r.data = body.data
        r.kind = body.kind
        r.updated_by = user.id
    else:
        # A record always lives with its patient's unit, so it can never drift to another ward.
        r = ClinicalRecord(
            id=body.id or None, unit_id=patient.unit_id, patient_id=body.patient_id,
            kind=body.kind, data=body.data, created_by=user.id, updated_by=user.id,
        )
        db.add(r)
    db.commit(); db.refresh(r)
    return _record_out(r)


@app.get("/api/records", response_model=list[ItemOut])
def list_records(patient_id: str, kind: str = "", user: User = Depends(current_user), db: Session = Depends(get_db)):
    patient = db.query(Patient).filter(Patient.id == patient_id).first()
    if not patient or patient.unit_id not in accessible_unit_ids(user, db):
        raise HTTPException(status_code=404, detail="Not found")
    q = db.query(ClinicalRecord).filter(
        ClinicalRecord.patient_id == patient_id,
        ClinicalRecord.deleted == False,  # noqa: E712
    )
    if kind:
        q = q.filter(ClinicalRecord.kind == kind)
    return [_record_out(r) for r in q.order_by(ClinicalRecord.updated_at.desc()).all()]


@app.delete("/api/records/{rid}")
def delete_record(rid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    r = db.query(ClinicalRecord).filter(ClinicalRecord.id == rid).first()
    if not r or r.unit_id not in accessible_unit_ids(user, db):
        raise HTTPException(status_code=404, detail="Not found")
    r.deleted = True
    r.updated_by = user.id
    db.commit()
    return {"ok": True}


# ------------------------------------------------------------------------------- sync
@app.get("/api/sync", response_model=SyncOut)
def sync(since: str = "", user: User = Depends(current_user), active: str | None = Depends(active_unit), db: Session = Depends(get_db)):
    """Everything in the viewed ward changed since `since` (ISO time). Powers live updates:
    the app calls this periodically to pull peers' changes. The ward is the one being viewed
    (active), else the user's home unit — so an admin's sync follows their ward switch."""
    units = [active] if active else user_unit_ids(user)
    cutoff = None
    if since:
        try:
            cutoff = dt.datetime.fromisoformat(since.replace("Z", ""))
        except ValueError:
            cutoff = None

    pq = db.query(Patient).filter(Patient.unit_id.in_(units))
    rq = db.query(ClinicalRecord).filter(ClinicalRecord.unit_id.in_(units))
    if cutoff:
        pq = pq.filter(Patient.updated_at > cutoff)
        rq = rq.filter(ClinicalRecord.updated_at > cutoff)

    return SyncOut(
        patients=[_patient_out(p) for p in pq.all()],
        records=[_record_out(r) for r in rq.all()],
        server_time=_iso(dt.datetime.utcnow()),
    )
