"""
Pydantic schemas — the shapes of what goes in and out of the API.
Kept deliberately small; clinical payloads are free-form JSON (`data`) so the server
stays generic and the app's own record shapes pass through unchanged.
"""
from typing import Any, Optional
from pydantic import BaseModel


# --- Auth ---
class LoginIn(BaseModel):
    username: str
    password: str


class UnitOut(BaseModel):
    id: str
    name: str


class UserOut(BaseModel):
    id: str
    username: str
    display_name: str
    role: str
    units: list[UnitOut]


class LoginOut(BaseModel):
    token: str
    user: UserOut


# --- Patients & clinical records (payload is free-form) ---
class PatientIn(BaseModel):
    id: Optional[str] = None          # app may supply its own uuid
    data: dict[str, Any]


class RecordIn(BaseModel):
    id: Optional[str] = None
    patient_id: str
    kind: str
    data: dict[str, Any]


class ItemOut(BaseModel):
    id: str
    unit_id: str
    data: dict[str, Any]
    kind: Optional[str] = None
    patient_id: Optional[str] = None
    deleted: bool
    updated_at: str
    created_by: Optional[str] = None
    updated_by: Optional[str] = None


class SyncOut(BaseModel):
    patients: list[ItemOut]
    records: list[ItemOut]
    server_time: str


# --- Admin (used in Stage 2, defined here for completeness) ---
class CreateUnitIn(BaseModel):
    name: str


class CreateUserIn(BaseModel):
    username: str
    password: str
    display_name: str = ""
    role: str = "user"
    unit_ids: list[str] = []


class SetActiveIn(BaseModel):
    active: bool


class ResetPasswordIn(BaseModel):
    password: str
