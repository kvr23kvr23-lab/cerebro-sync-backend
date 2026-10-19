"""
Web Push — notifications that reach a phone even when Cerebro is closed.

Flow:
  - Each device subscribes (POST /api/push/subscribe); we store its push endpoint + keys.
  - An external cron pings POST/GET /api/push/run?secret=... every minute. That scan looks
    through every unit's patients for follow-ups now due (remindOn + a followUpAt that has
    passed, still open) and sends a push to that unit's devices.
  - public/sw.js shows the notification and opens the patient's reminders on tap.

Server env vars:
  VAPID_PRIVATE_KEY  — push signing key: the PEM, or a single-line base64 of the PEM
  VAPID_SUBJECT      — a contact, e.g. "mailto:you@example.com" (optional; has a default)
  PUSH_RUN_SECRET    — the secret the cron must pass as ?secret=... to run the scan

If VAPID keys are absent the endpoints return 503 and nothing else is affected.
"""
import os
import json
import base64
import datetime as dt
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .db import get_db
from .models import Patient, PushSubscription, PushSent, User
from .deps import current_user, user_unit_ids

router = APIRouter(prefix="/api/push", tags=["push"])

CLOSED_STATUSES = {"done", "resolved", "reported", "cancelled", "complete", "completed", "closed"}
OVERDUE_WINDOW_MS = 7 * 86400 * 1000  # don't blast follow-ups more than 7 days overdue


def _private_pem():
    raw = (os.environ.get("VAPID_PRIVATE_KEY") or "").strip()
    if not raw:
        return None
    if "BEGIN" in raw:
        return raw
    try:
        return base64.b64decode(raw).decode()
    except Exception:
        return None


def _subject():
    return os.environ.get("VAPID_SUBJECT", "mailto:admin@cerebro.app")


def _app_server_key(pem: str) -> str:
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    from cryptography.hazmat.primitives import serialization
    priv = load_pem_private_key(pem.encode(), password=None)
    point = priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return base64.urlsafe_b64encode(point).decode().rstrip("=")


# ------------------------------------------------------------------------------- key
@router.get("/key")
def get_key():
    pem = _private_pem()
    if not pem:
        raise HTTPException(status_code=503, detail="Push is not configured on the server.")
    try:
        return {"key": _app_server_key(pem)}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Push key error: {e}")


# ------------------------------------------------------------------------- subscribe
class SubIn(BaseModel):
    endpoint: str
    keys: dict = {}


@router.post("/subscribe")
def subscribe(body: SubIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    units = user_unit_ids(user)
    if not units:
        raise HTTPException(status_code=403, detail="You are not assigned to a unit.")
    p256dh = (body.keys or {}).get("p256dh")
    auth = (body.keys or {}).get("auth")
    if not body.endpoint or not p256dh or not auth:
        raise HTTPException(status_code=400, detail="Incomplete subscription.")
    row = db.query(PushSubscription).filter(PushSubscription.endpoint == body.endpoint).first()
    if row:
        row.unit_id = units[0]; row.user_id = user.id; row.p256dh = p256dh; row.auth = auth
    else:
        db.add(PushSubscription(unit_id=units[0], user_id=user.id, endpoint=body.endpoint, p256dh=p256dh, auth=auth))
    db.commit()
    return {"ok": True}


class UnsubIn(BaseModel):
    endpoint: str


@router.post("/unsubscribe")
def unsubscribe(body: UnsubIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    db.query(PushSubscription).filter(PushSubscription.endpoint == body.endpoint).delete()
    db.commit()
    return {"ok": True}


# -------------------------------------------------------------------------------- run
def _parse_ms(s):
    s = str(s).strip()
    try:
        if s.endswith("Z"):
            d = dt.datetime.fromisoformat(s[:-1]).replace(tzinfo=dt.timezone.utc)
        else:
            d = dt.datetime.fromisoformat(s)
            if d.tzinfo is None:
                d = d.replace(tzinfo=dt.timezone.utc)
        return d.timestamp() * 1000
    except Exception:
        return None


def _due_followups(data: dict, now_ms: float):
    """List (inv_id, label, followUpAt) for each due, still-open follow-up in a bundle."""
    out = []
    for inv in (data or {}).get("investigations") or []:
        if not isinstance(inv, dict) or not inv.get("remindOn"):
            continue
        fa = inv.get("followUpAt")
        if not fa:
            continue
        if str(inv.get("status") or "").lower() in CLOSED_STATUSES:
            continue
        t = _parse_ms(fa)
        if t is None or t > now_ms or (now_ms - t) > OVERDUE_WINDOW_MS:
            continue
        label = inv.get("subtype") or inv.get("type") or inv.get("name") or "Follow-up"
        out.append((inv.get("id") or str(fa), str(label), str(fa)))
    return out


def _send(sub: PushSubscription, title: str, body: str, url: str, key_path: str, db: Session):
    """Returns None on success, or an error string (so the scan can report why it failed)."""
    from pywebpush import webpush, WebPushException
    payload = json.dumps({"title": title, "body": body, "url": url, "tag": "cerebro-followup"})
    try:
        webpush(
            subscription_info={"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}},
            data=payload,
            vapid_private_key=key_path,  # a PEM file path — pywebpush loads it reliably
            vapid_claims={"sub": _subject()},
            ttl=3600,
        )
        return None
    except WebPushException as e:
        code = getattr(getattr(e, "response", None), "status_code", None)
        if code in (404, 410):  # subscription gone — drop it
            db.query(PushSubscription).filter(PushSubscription.id == sub.id).delete()
            db.commit()
        return f"WebPushException code={code}: {e}"
    except Exception as e:
        return f"{type(e).__name__}: {e}"


@router.post("/run")
@router.get("/run")
def run(request: Request, db: Session = Depends(get_db)):
    secret = os.environ.get("PUSH_RUN_SECRET", "")
    if not secret or request.query_params.get("secret") != secret:
        raise HTTPException(status_code=403, detail="Bad or missing secret.")
    pem = _private_pem()
    if not pem:
        raise HTTPException(status_code=503, detail="Push not configured.")
    now_ms = dt.datetime.now(dt.timezone.utc).timestamp() * 1000

    subs_by_unit = {}
    for s in db.query(PushSubscription).all():
        subs_by_unit.setdefault(s.unit_id, []).append(s)
    if not subs_by_unit:
        return {"ok": True, "sent": 0, "note": "no subscriptions"}

    # Write the PEM to a temp file once; pywebpush loads a PEM *file* reliably across versions.
    import tempfile
    fd, key_path = tempfile.mkstemp(suffix=".pem")
    with os.fdopen(fd, "w") as f:
        f.write(pem)

    sent = 0
    due_scanned = 0
    errors = []
    try:
        for p in db.query(Patient).filter(Patient.deleted == False).all():  # noqa: E712
            subs = subs_by_unit.get(p.unit_id)
            if not subs:
                continue
            data = p.data or {}
            identity = data.get("patient") or {}
            name = identity.get("name") or "Patient"
            bed = identity.get("bedNumber")
            for inv_id, label, fa in _due_followups(data, now_ms):
                due_scanned += 1
                title = f"Cerebro · {name}" + (f" (bed {bed})" if bed else "")
                body = f"{label} — follow-up due"
                for s in subs:
                    key = f"{s.id}|{inv_id}|{fa}"
                    if db.query(PushSent).filter(PushSent.dedup_key == key).first():
                        continue
                    err = _send(s, title, body, "/reminders", key_path, db)
                    if err is None:
                        db.add(PushSent(dedup_key=key))
                        db.commit()
                        sent += 1
                    elif len(errors) < 5:
                        errors.append(err)
    finally:
        try:
            os.remove(key_path)
        except Exception:
            pass

    try:
        cutoff = dt.datetime.utcnow() - dt.timedelta(days=30)
        db.query(PushSent).filter(PushSent.created_at < cutoff).delete()
        db.commit()
    except Exception:
        db.rollback()

    return {"ok": True, "sent": sent, "due_scanned": due_scanned, "errors": errors}


@router.get("/debug")
def debug(request: Request, db: Session = Depends(get_db)):
    """Temporary diagnostic: what the scan actually sees on the server. Secret-protected."""
    secret = os.environ.get("PUSH_RUN_SECRET", "")
    if not secret or request.query_params.get("secret") != secret:
        raise HTTPException(status_code=403, detail="Bad or missing secret.")
    now_ms = dt.datetime.now(dt.timezone.utc).timestamp() * 1000
    subs = db.query(PushSubscription).all()
    out = {
        "now_utc_ms": now_ms,
        "subscriptions": [{"unit_id": s.unit_id, "user_id": s.user_id, "endpoint_host": (s.endpoint or "")[:40]} for s in subs],
        "patients": [],
    }
    for p in db.query(Patient).filter(Patient.deleted == False).all():  # noqa: E712
        data = p.data or {}
        invs = data.get("investigations")
        rec = {
            "unit_id": p.unit_id,
            "name": (data.get("patient") or {}).get("name"),
            "bundle_keys": sorted(list(data.keys()))[:30],
            "investigations_type": type(invs).__name__,
            "investigations_count": len(invs) if isinstance(invs, list) else None,
            "reminders": [],
        }
        if isinstance(invs, list):
            for inv in invs:
                if not isinstance(inv, dict):
                    continue
                if inv.get("remindOn") or inv.get("followUpAt"):
                    t = _parse_ms(inv.get("followUpAt")) if inv.get("followUpAt") else None
                    rec["reminders"].append({
                        "id": inv.get("id"),
                        "keys": sorted(list(inv.keys()))[:20],
                        "remindOn": inv.get("remindOn"),
                        "followUpAt": inv.get("followUpAt"),
                        "status": inv.get("status"),
                        "parsed_ms": t,
                        "due": (t is not None and t <= now_ms and (now_ms - t) <= OVERDUE_WINDOW_MS),
                    })
        out["patients"].append(rec)
    return out
