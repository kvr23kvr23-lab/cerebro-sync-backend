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
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")


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
    template: str = ""   # optional discharge FORMAT to follow exactly (mode == "discharge")


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
        tpl = (body.template or "").strip()
        if tpl:
            system = SAFETY + (
                " Produce a hospital DISCHARGE SUMMARY that follows the clinician's FORMAT EXACTLY: "
                "use their section headings, in their order, and fill each section from the record. "
                "Do not add, rename, reorder or drop sections. If the record lacks information for a "
                "section, write a brief 'Not documented.' under it rather than inventing anything. "
                "Keep any fixed wording the format includes. Never present a provisional diagnosis as "
                "confirmed — label uncertainty explicitly."
            )
            user = (f"De-identified patient record (JSON):\n{record_json}\n\n"
                    f"Discharge summary FORMAT to follow exactly:\n{tpl}\n\n"
                    "Write the discharge summary in this exact format.")
        else:
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


# ------------------------------------------------------------------- /history-review
# Drafts the history summary AND checks completeness: what important history points are not
# yet documented, and what to ask to fill them. History only — never comments on exam/dx.
HISTORY_REVIEW_SYSTEM = (
    SAFETY +
    " You are reviewing a neurology HISTORY only (not examination, investigations or diagnosis). "
    "Return STRICT JSON only (no prose, no markdown fences): "
    '{"summary":"", "missing":[""], "questions":[""]}. '
    "summary: a concise 4-7 line history summary (prose, no headings) — presenting syndrome & "
    "duration, onset & course, key positives, important negatives, relevant background, and any "
    "unresolved points within the history. Do NOT mention examination, investigations, diagnosis "
    "or differential. "
    "missing: important history points NOT yet documented that matter for THIS presentation "
    "(short phrases, most important first). "
    "questions: specific questions to ask the patient or informant to fill those gaps. "
    "Base everything ONLY on the history provided; never invent findings. Use [] where nothing applies."
)


class HistoryReviewIn(BaseModel):
    record: dict[str, Any] = {}


@router.post("/api/ai/history-review")
def history_review(body: HistoryReviewIn):
    record_json = json.dumps(body.record, ensure_ascii=False, indent=2)
    user = f"De-identified HISTORY record (JSON):\n{record_json}\n\nReturn the JSON."
    text = _complete(HISTORY_REVIEW_SYSTEM, user, max_tokens=1200)
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    try:
        data = json.loads(cleaned)
    except Exception:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        data = json.loads(cleaned[start:end + 1]) if start >= 0 and end > start else {}

    def _list(key):
        return [str(x).strip() for x in (data.get(key) or []) if str(x).strip()][:8]

    return {
        "summary": (data.get("summary") or "").strip(),
        "missing": _list("missing"),
        "questions": _list("questions"),
    }


@router.get("/api/ai/health")
def ai_health():
    return {"ai_configured": bool(os.environ.get("ANTHROPIC_API_KEY")), "model": MODEL,
            "time": dt.datetime.utcnow().isoformat() + "Z"}


# ----------------------------------------------------------------------- /literature
# Case Intelligence, slice 1: turn the de-identified case into a focused query and pull
# published articles / case reports from Europe PMC (indexes PubMed + more; free, no key).
# The LLM only ever sees the de-identified record; Europe PMC only sees the query string.
EPMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

QUERY_SYSTEM = (
    "You turn a de-identified neurology case into ONE focused literature-search query for "
    "Europe PMC (which indexes PubMed). Output ONLY the query string — no prose, no quotes, "
    "no explanation, one line. Use the diagnosis/syndrome plus 1-3 key distinguishing features; "
    "join concepts with AND and synonyms with OR. Keep it short. "
    "Example: guillain-barre syndrome AND (axonal OR AMAN) AND nerve conduction"
)


class LiteratureIn(BaseModel):
    record: dict[str, Any] = {}
    query: str = ""          # optional: clinician-supplied query overrides the generated one
    cursor: str = "*"        # Europe PMC cursorMark; "*" is the first page
    page_size: int = 5


def _build_query(record: dict, manual: str) -> str:
    if (manual or "").strip():
        return " ".join(manual.split())[:300]
    record_json = json.dumps(record, ensure_ascii=False)
    q = _complete(QUERY_SYSTEM, f"De-identified case (JSON):\n{record_json}\n\nSearch query:", max_tokens=120)
    q = " ".join((q or "").split())[:300]
    return q or "neurology"


def _epmc_article(r: dict) -> dict:
    pmid = r.get("pmid")
    doi = r.get("doi")
    src = r.get("source") or "MED"
    rid = r.get("id") or ""
    if pmid:
        url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
    elif doi:
        url = f"https://doi.org/{doi}"
    else:
        url = f"https://europepmc.org/article/{src}/{rid}"
    journal = r.get("journalTitle") or ((r.get("journalInfo") or {}).get("journal") or {}).get("title") or ""
    abstract = (r.get("abstractText") or "").strip()
    # abstractText can carry light HTML tags from Europe PMC; strip the common ones.
    for tag in ("<p>", "</p>", "<i>", "</i>", "<b>", "</b>", "<sup>", "</sup>", "<sub>", "</sub>"):
        abstract = abstract.replace(tag, "")
    if len(abstract) > 300:
        abstract = abstract[:300].rstrip() + "…"
    return {
        "id": f"{src}:{rid}",
        "title": (r.get("title") or "(untitled)").rstrip("."),
        "authors": r.get("authorString") or "",
        "journal": journal,
        "year": str(r.get("pubYear") or ""),
        "isOpenAccess": (r.get("isOpenAccess") == "Y"),
        "url": url,
        "pmid": pmid or "",
        "doi": doi or "",
        "abstract": abstract,
    }


@router.post("/api/ai/literature")
def literature(body: LiteratureIn):
    query = _build_query(body.record, body.query)
    params = {
        "query": query,
        "format": "json",
        "resultType": "core",       # includes abstractText + journal info
        "pageSize": max(1, min(body.page_size or 5, 10)),
        "cursorMark": body.cursor or "*",
    }
    try:
        import httpx
        with httpx.Client(timeout=20) as c:
            resp = c.get(EPMC_SEARCH, params=params, headers={"Accept": "application/json"})
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Literature search failed: {e}")
    results = (data.get("resultList") or {}).get("result") or []
    articles = [_epmc_article(r) for r in results]
    next_cursor = data.get("nextCursorMark") or ""
    has_more = bool(next_cursor and next_cursor != (body.cursor or "*") and len(articles) >= params["pageSize"])
    return {
        "query": query,
        "articles": articles,
        "total": data.get("hitCount", 0),
        "nextCursor": next_cursor,
        "hasMore": has_more,
    }
