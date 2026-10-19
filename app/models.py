"""
Database models — the shape of the shared data.

Mirrors CEREBRO_MULTIUSER_ARCHITECTURE.md:
  Institution (implicit — one per server) → Units → Users → Patients → clinical records.

Design notes:
- Every clinical record carries `unit_id` (enforces isolation: a user only ever receives
  their unit's data) and `updated_at` (powers the "give me everything changed since X" sync).
- Clinical sub-records store their payload as JSON (`data`) rather than dozens of columns.
  This keeps the server generic — it doesn't need to know every clinical field — and lets
  the frontend's existing record shapes flow through unchanged. The server's job is
  identity, access-control and sync; the clinical meaning stays in the app.
- Nothing is hard-deleted: `deleted` flags keep history and let sync tell peers "this was
  removed" (Decision: never destroy objective clinical data).
"""
import uuid
import datetime as dt
from sqlalchemy import (
    Column, String, Boolean, DateTime, ForeignKey, Text, JSON, Table, Index
)
from sqlalchemy.orm import relationship
from .db import Base


def _uuid():
    return str(uuid.uuid4())


def _now():
    return dt.datetime.utcnow()


# --- Users ↔ Units (many-to-many: a user can belong to one or more units) ---
user_units = Table(
    "user_units", Base.metadata,
    Column("user_id", String, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("unit_id", String, ForeignKey("units.id", ondelete="CASCADE"), primary_key=True),
)


class Unit(Base):
    __tablename__ = "units"
    id = Column(String, primary_key=True, default=_uuid)
    name = Column(String, nullable=False)
    created_at = Column(DateTime, default=_now)

    users = relationship("User", secondary=user_units, back_populates="units")


class User(Base):
    __tablename__ = "users"
    id = Column(String, primary_key=True, default=_uuid)
    username = Column(String, unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=False)
    display_name = Column(String, default="")
    role = Column(String, default="user")        # "user" | "admin"
    active = Column(Boolean, default=True)        # False = disabled, can't log in
    created_at = Column(DateTime, default=_now)

    units = relationship("Unit", secondary=user_units, back_populates="users")


class Patient(Base):
    __tablename__ = "patients"
    id = Column(String, primary_key=True, default=_uuid)
    unit_id = Column(String, ForeignKey("units.id"), nullable=False, index=True)
    data = Column(JSON, nullable=False, default=dict)   # name, age, bed, diagnosis, etc.
    created_by = Column(String, ForeignKey("users.id"), nullable=True)
    updated_by = Column(String, ForeignKey("users.id"), nullable=True)
    deleted = Column(Boolean, default=False)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now, index=True)


class ClinicalRecord(Base):
    """
    One table for all clinical sub-records (notes, investigations, medications, problems,
    exam, history, discharge, etc.), distinguished by `kind`. Generic by design: the server
    stores and syncs them without needing to understand each clinical type.
    """
    __tablename__ = "clinical_records"
    id = Column(String, primary_key=True, default=_uuid)
    unit_id = Column(String, ForeignKey("units.id"), nullable=False, index=True)
    patient_id = Column(String, ForeignKey("patients.id"), nullable=False, index=True)
    kind = Column(String, nullable=False, index=True)   # "note" | "investigation" | "medication" | ...
    data = Column(JSON, nullable=False, default=dict)
    created_by = Column(String, ForeignKey("users.id"), nullable=True)
    updated_by = Column(String, ForeignKey("users.id"), nullable=True)
    deleted = Column(Boolean, default=False)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now, index=True)


class PushSubscription(Base):
    """A device's Web Push subscription — one per browser/device. Scoped to the user's
    unit so a due follow-up pushes to that unit's devices. Stored so the server can wake
    a phone even when Cerebro is closed."""
    __tablename__ = "push_subscriptions"
    id = Column(String, primary_key=True, default=_uuid)
    unit_id = Column(String, ForeignKey("units.id"), nullable=False, index=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    endpoint = Column(Text, nullable=False, unique=True)
    p256dh = Column(String, nullable=False)
    auth = Column(String, nullable=False)
    created_at = Column(DateTime, default=_now)


class PushSent(Base):
    """Dedup log: one row per (subscription, follow-up occurrence) already pushed, so the
    minute-by-minute scan never re-notifies the same reminder. Pruned after 30 days."""
    __tablename__ = "push_sent"
    id = Column(String, primary_key=True, default=_uuid)
    dedup_key = Column(String, nullable=False, unique=True, index=True)
    created_at = Column(DateTime, default=_now, index=True)


# Composite indexes that make the common queries fast:
#  - "all patients in my unit, changed since X"
Index("ix_patients_unit_updated", Patient.unit_id, Patient.updated_at)
#  - "all clinical records in my unit, changed since X"
Index("ix_records_unit_updated", ClinicalRecord.unit_id, ClinicalRecord.updated_at)
#  - "all records for one patient of a given kind"
Index("ix_records_patient_kind", ClinicalRecord.patient_id, ClinicalRecord.kind)
