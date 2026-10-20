"""
Discharge templates — named discharge-summary FORMATS, shared across the institution.

Any logged-in clinician can LIST templates (to generate a summary in that format); only an
admin can create, edit or delete them. A template is a format only — headings/structure and
any fixed wording — never patient data.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .db import get_db
from .models import User, DischargeTemplate
from .deps import current_user, require_admin

router = APIRouter(prefix="/api/templates", tags=["templates"])


def _out(t: DischargeTemplate) -> dict:
    return {"id": t.id, "name": t.name, "body": t.body or ""}


class TemplateIn(BaseModel):
    name: str
    body: str = ""


@router.get("/discharge")
def list_discharge(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.query(DischargeTemplate).order_by(DischargeTemplate.name).all()
    return [_out(t) for t in rows]


@router.post("/discharge")
def create_discharge(body: TemplateIn, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="A name is required.")
    t = DischargeTemplate(name=name, body=body.body or "", created_by=admin.id)
    db.add(t); db.commit(); db.refresh(t)
    return _out(t)


@router.post("/discharge/{tid}")
def update_discharge(tid: str, body: TemplateIn, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    t = db.query(DischargeTemplate).filter(DischargeTemplate.id == tid).first()
    if not t:
        raise HTTPException(status_code=404, detail="Template not found.")
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="A name is required.")
    t.name = name
    t.body = body.body or ""
    db.commit()
    return _out(t)


@router.delete("/discharge/{tid}")
def delete_discharge(tid: str, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    t = db.query(DischargeTemplate).filter(DischargeTemplate.id == tid).first()
    if not t:
        raise HTTPException(status_code=404, detail="Template not found.")
    db.delete(t); db.commit()
    return {"ok": True}
