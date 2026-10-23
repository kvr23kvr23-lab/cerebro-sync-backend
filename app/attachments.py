"""
Attachment blobs — the file bytes behind a patient's attachments (investigation photos/PDFs).

The attachment METADATA (id, filename, which investigation) rides inside the patient bundle and
syncs like everything else. Only the bytes live here, so photos work across the team in shared
mode. Everything is scoped to the patient's unit; an admin (institution-wide) may reach any unit,
others only their own — the same access rule as patients.
"""
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Response
from sqlalchemy.orm import Session

from .db import get_db
from .models import User, Patient, AttachmentBlob
from .deps import current_user, accessible_unit_ids

router = APIRouter(prefix="/api/attachments", tags=["attachments"])

MAX_BYTES = 10 * 1024 * 1024  # 10 MB per file (matches the client-side cap)


def _patient_or_403(db: Session, user: User, patient_id: str) -> Patient:
    p = db.query(Patient).filter(Patient.id == patient_id).first()
    if not p or p.unit_id not in accessible_unit_ids(user, db):
        raise HTTPException(status_code=404, detail="Patient not found")
    return p


@router.post("/{aid}")
async def put_blob(
    aid: str,
    patient_id: str = Form(...),
    file: UploadFile = File(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    p = _patient_or_403(db, user, patient_id)
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file.")
    if len(raw) > MAX_BYTES:
        raise HTTPException(status_code=400, detail="File too large (max 10MB).")
    ctype = file.content_type or "application/octet-stream"
    row = db.query(AttachmentBlob).filter(AttachmentBlob.id == aid).first()
    if row:
        if row.unit_id not in accessible_unit_ids(user, db):
            raise HTTPException(status_code=404, detail="Not found")
        row.data = raw; row.size = len(raw); row.content_type = ctype
        row.patient_id = patient_id; row.unit_id = p.unit_id
    else:
        db.add(AttachmentBlob(
            id=aid, unit_id=p.unit_id, patient_id=patient_id,
            content_type=ctype, size=len(raw), data=raw, created_by=user.id,
        ))
    db.commit()
    return {"ok": True, "id": aid, "size": len(raw), "content_type": ctype}


@router.get("/{aid}")
def get_blob(aid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = db.query(AttachmentBlob).filter(AttachmentBlob.id == aid).first()
    if not row or row.unit_id not in accessible_unit_ids(user, db):
        raise HTTPException(status_code=404, detail="Not found")
    return Response(
        content=row.data,
        media_type=row.content_type or "application/octet-stream",
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.delete("/{aid}")
def delete_blob(aid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = db.query(AttachmentBlob).filter(AttachmentBlob.id == aid).first()
    if not row:
        return {"ok": True}
    if row.unit_id not in accessible_unit_ids(user, db):
        raise HTTPException(status_code=404, detail="Not found")
    db.delete(row); db.commit()
    return {"ok": True}
