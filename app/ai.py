"""
AI endpoints — the "Draft with AI" and note auto-extract features.

These run on the SAME server as sync (one deployment does both). They call Anthropic's API
using ANTHROPIC_API_KEY from the environment. The frontend only ever sends DE-IDENTIFIED
text (name/bed/hospital-number/consultant are stripped on the device before anything is
sent), so no patient identifiers reach this endpoint.

Endpoints:
  POST /api/ai/discuss  { record, query, mode }  -> { "summary": "<text>" }
  POST /api/ai/extract  { note, context }        -> { investigations[], medications[], problems[] }

If ANTHROPIC_API_KEY is not set, both return 503 with a clear message, and the app simply
shows "Online AI isn't set up" — everything non-AI keeps working.
"""
import os
import json
import datetime as dt
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Any, Optional

router = APIRouter()

# Model is configurable; a sensible current default. Change ANTHROPIC_MODEL to use another.
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest")


def _client():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise HTTPException(status_code=503, detail="Online AI isn't configured on the server (no API key).")
    try:
        from anthropic import Anthropic
    except Exception:
        raise HTTPException(status_code=503, detail="AI library not installed on the server.")
    return Anthropic(api_key=key)


def _complete(system: str, user: str, max_tokens: int = 1500) -> str:
    client = _client()
    try:
        msg = client.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except Exception as e:
        # Surface a readable error (e.g. credit/balance, rate limit) to the app.
        raise HTTPException(status_code=502, detail=f"AI request failed: {e}")
    parts = []
    for block in (msg.content or []):
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "".join(parts).strip()


# --------------------------------------------------------------------------- /discuss
class DiscussIn(BaseModel):
    record: dict[str, Any] = {}
    query: str = ""
    mode: str = "discuss"


SAFETY = (
    "You are a clinical documentation aid for a neurologist, not a decision-maker. "
    "Use ONLY the de-identified information provided; never invent findings, numbers or history. "
    "Never present a provisional/working diagnosis as confirmed — label uncertainty explicitly. "
    "Do not include any patient identifiers. Write in clear, concise clinical prose."
)

DISCHARGE_SYSTEM = SAFETY + (
    " Produce a hospital DISCHARGE SUMMARY with these eight sections, each as a heading: "
    "1) Patient & admission details (as available), 2) Presenting complaints, "
    "3) Working/Provisional Diagnosis (discuss the differentials considered and why), "
    "4) Clinical course in hospital, 5) Investigations and results, 6) Treatment given, "
    "7) Condition at discharge & functional status, 8) Discharge medications and follow-up/advice. "
    "If the diagnosis is not confirmed, title section 3 'Working/Provisional Diagnosis' and make the "
    "uncertainty and the differential reasoning explicit — never state a provisional diagnosis as final."
)


@router.post("/api/ai/discuss")
def discuss(body: DiscussIn):
    record_json = json.dumps(body.record, ensure_ascii=False, indent=2)
    mode = (body.mode or "discuss").lower()
    if mode == "discharge":
        system = DISCHARGE_SYSTEM
        user = f"De-identified patient record (JSON):\n{record_json}\n\nWrite the discharge summary."
    elif body.query:
        system = SAFETY
        user = (f"De-identified patient record (JSON):\n{record_json}\n\n"
                f"Task:\n{body.query}")
    else:
        system = SAFETY
        user = (f"De-identified patient record (JSON):\n{record_json}\n\n"
                "Give a concise clinical case summary for the top of the record.")
    text = _complete(system, user, max_tokens=1800)
    return {"summary": text}


# --------------------------------------------------------------------------- /extract
class ExtractIn(BaseModel):
    note: str = ""
    context: dict[str, Any] = {}


EXTRACT_SYSTEM = (
    "You extract structured orders from a de-identified neurology ward-round note. "
    "Return STRICT JSON only (no prose, no markdown fences) with exactly these keys: "
    '{"investigations":[{"name":"","status":"ordered|pending|done","result":""}],'
    '"medications":[{"name":"","action":"started|stopped|changed|continued","detail":""}],'
    '"problems":[{"title":"","kind":"active|resolved"}]}. '
    "Only include items clearly mentioned in the note. If a list is empty, use []. "
    "Do not invent anything; copy names faithfully. Never include identifiers."
)


@router.post("/api/ai/extract")
def extract(body: ExtractIn):
    if not (body.note or "").strip():
        return {"investigations": [], "medications": [], "problems": []}
    user = f"Note:\n{body.note}\n\nReturn the JSON."
    text = _complete(EXTRACT_SYSTEM, user, max_tokens=800)
    # The model should return pure JSON; be tolerant of stray fencing.
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    try:
        data = json.loads(cleaned)
    except Exception:
        # Last resort: find the first {...} block.
        start, end = cleaned.find("{"), cleaned.rfind("}")
        data = json.loads(cleaned[start:end + 1]) if start >= 0 and end > start else {}
    return {
        "investigations": data.get("investigations", []) or [],
        "medications": data.get("medications", []) or [],
        "problems": data.get("problems", []) or [],
    }


@router.get("/api/ai/health")
def ai_health():
    return {"ai_configured": bool(os.environ.get("ANTHROPIC_API_KEY")), "model": MODEL,
            "time": dt.datetime.utcnow().isoformat() + "Z"}
