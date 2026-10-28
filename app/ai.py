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
import base64
import datetime as dt
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import StreamingResponse
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


# --------------------------------------------------------------------------------- /ocr
# Reads text out of a photo (a report, a referral note, handwritten notes) so the clinician
# can snap-and-paste instead of typing. Used by the camera button on history/exam/reasoning
# and the investigation Result field. NOTE: unlike the text endpoints, an image cannot be
# de-identified before sending — the clinician controls what they photograph.
OCR_SYSTEM = (
    "You are an OCR engine. Transcribe all text visible in the image exactly as written, "
    "preserving line breaks, numbers and units. Output ONLY the transcribed text — no "
    "commentary, no translation, no extra formatting. If there is no legible text, output nothing."
)
_OCR_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


@router.post("/api/ocr")
async def ocr(image: UploadFile = File(...)):
    client = _client()  # 503 if no API key
    raw = await image.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty image.")
    if len(raw) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Image too large (max 10MB).")
    media = image.content_type if image.content_type in _OCR_TYPES else "image/jpeg"
    b64 = base64.b64encode(raw).decode()
    try:
        msg = client.messages.create(
            model=MODEL,
            max_tokens=2000,
            system=OCR_SYSTEM,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media, "data": b64}},
                {"type": "text", "text": "Transcribe the text in this image."},
            ]}],
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"OCR failed: {e}")
    parts = [b.text for b in (msg.content or []) if getattr(b, "type", None) == "text"]
    return {"text": "".join(parts).strip()}


# ----------------------------------------------------------------------- /workup
# Suggests a focused workup for a problem/abnormality (the AI fallback for problems the
# clinician's curated list doesn't cover). Takes only the problem text + a short de-identified
# context — no identifiers. AI proposes; the clinician selects and orders.
WORKUP_SYSTEM = (
    SAFETY +
    " Given a neurology inpatient problem/abnormality, list the most useful NEXT investigations "
    "to work it up. High-yield and guideline-aligned; no scatter-gun. Return STRICT JSON only "
    '(no prose, no fences): {"tests":["",""]}. Each item is a concise test name (what you would '
    "write on an order), 4-8 items max. If little applies, return a short list or []."
)


class WorkupIn(BaseModel):
    problem: str = ""
    context: str = ""   # optional de-identified one-liner (e.g. "58M, working dx: vasculitis")


@router.post("/api/ai/workup")
def workup(body: WorkupIn):
    problem = (body.problem or "").strip()
    if not problem:
        return {"tests": []}
    ctx = (body.context or "").strip()
    user = f"Problem/abnormality: {problem}" + (f"\nContext: {ctx}" if ctx else "") + "\n\nReturn the JSON."
    text = _complete(WORKUP_SYSTEM, user, max_tokens=500)
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
    tests = [str(x).strip() for x in (data.get("tests") or []) if str(x).strip()][:8]
    return {"tests": tests}


# ------------------------------------------------------------------- /extract-panel
# Reads a photographed report and fills a given set of panel fields (e.g. CSF: opening
# pressure, cells, protein, glucose…). Returns only fields it can confidently read.
@router.post("/api/ai/extract-panel")
async def extract_panel(fields: str = Form("[]"), image: UploadFile = File(...)):
    client = _client()  # 503 if no key
    raw = await image.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty image.")
    if len(raw) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Image too large (max 10MB).")
    media = image.content_type if image.content_type in _OCR_TYPES else "image/jpeg"
    b64 = base64.b64encode(raw).decode()
    try:
        flds = json.loads(fields) or []
    except Exception:
        flds = []
    valid_keys = {f.get("k") for f in flds if isinstance(f, dict) and f.get("k")}
    field_list = "\n".join(
        f"- {f.get('k')}: {f.get('label')}" for f in flds if isinstance(f, dict) and f.get("k")
    )
    system = (
        "You read a photographed lab / investigation report and extract values for a GIVEN list "
        "of fields. Return STRICT JSON only (no prose, no fences): "
        '{"values":{"<key>":"<value>"}}. Include a key ONLY if you can read its value from the '
        "image with confidence; use the EXACT field keys given. Values are short strings exactly as "
        "reported (keep units). Never invent or infer a value that is not visibly in the image. "
        "Omit anything you cannot read."
    )
    user_text = f"Fields (key: meaning):\n{field_list}\n\nRead the report image and return the JSON."
    try:
        msg = client.messages.create(
            model=MODEL, max_tokens=1200, system=system,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media, "data": b64}},
                {"type": "text", "text": user_text},
            ]}],
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Scan failed: {e}")
    text = "".join(b.text for b in (msg.content or []) if getattr(b, "type", None) == "text").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        data = json.loads(text)
    except Exception:
        start, end = text.find("{"), text.rfind("}")
        data = json.loads(text[start:end + 1]) if start >= 0 and end > start else {}
    vals = data.get("values") or {}
    out = {k: str(v).strip() for k, v in vals.items() if k in valid_keys and str(v).strip()}
    return {"values": out}


# ----------------------------------------------------------------------- /expand
# Turns the clinician's shorthand/keywords into concise clinical prose for a history section.
EXPAND_SYSTEM = (
    "You turn a neurologist's shorthand notes into concise, grammatical clinical prose for a "
    "history. Expand abbreviations naturally (wk→weakness, b/l→bilateral, h/o→history of, etc.) "
    "and write in clear sentences a clinician would document. Be FAITHFUL — never add symptoms, "
    "findings, times or negatives that are not in the notes. Keep it brief; no headings, no "
    "preamble, output only the prose. If a section name is given, write in that section's style."
)


class ExpandIn(BaseModel):
    text: str = ""
    section: str = ""


@router.post("/api/ai/expand")
def expand(body: ExpandIn):
    text = (body.text or "").strip()
    if not text:
        return {"text": ""}
    sec = (body.section or "").strip()
    user = (f"Section: {sec}\n" if sec else "") + f"Shorthand notes:\n{text}\n\nWrite the clinical prose."
    out = _complete(EXPAND_SYSTEM, user, max_tokens=700)
    return {"text": out}


# ----------------------------------------------------------------------- /revise
# Revise a generated document (e.g. a discharge summary) per the clinician's instruction.
REVISE_SYSTEM = (
    "You revise a clinical document per the clinician's instruction. Apply ONLY the requested "
    "change and keep everything else intact and faithful — do not invent findings or drop content "
    "that wasn't asked to be removed. Preserve the document's format/headings. Output ONLY the full "
    "revised document, no preamble, no commentary."
)


class ReviseIn(BaseModel):
    text: str = ""
    instruction: str = ""


@router.post("/api/ai/revise")
def revise(body: ReviseIn):
    text = (body.text or "").strip()
    instr = (body.instruction or "").strip()
    if not text or not instr:
        return {"text": text}
    user = f"Document:\n{text}\n\nRequested change:\n{instr}\n\nReturn the full revised document."
    out = _complete(REVISE_SYSTEM, user, max_tokens=2000)
    return {"text": out}


# ----------------------------------------------------------------------- /chat
# A multi-turn case discussion: the de-identified record is the context; the clinician and the
# assistant converse. Used by the Case chat tab (impression + back-and-forth).
CHAT_SYSTEM = (
    SAFETY +
    " You are discussing a de-identified neurology case with the clinician, as a knowledgeable "
    "colleague and teacher. Reason through localisation, differentials and next steps; give a clear "
    "impression; answer follow-ups directly; cite guideline-level evidence or key trials where it "
    "helps. Use ONLY the record provided plus the clinician's messages — never invent findings, "
    "numbers or identifiers, and label uncertainty.\n"
    "STYLE: be concise and well-structured — a focused impression, not an exhaustive essay. Prefer "
    "short paragraphs or tight bullet points. Keep the OPENING impression to roughly 150-250 words; "
    "keep follow-up answers shorter still. Always finish your last sentence — never stop mid-thought."
)


class ChatIn(BaseModel):
    record: dict[str, Any] = {}
    messages: list[dict[str, Any]] = []   # [{role: "user"|"assistant", content}]


@router.post("/api/ai/chat")
def chat(body: ChatIn):
    record_json = json.dumps(body.record, ensure_ascii=False, indent=2)
    system = CHAT_SYSTEM + "\n\nDe-identified patient record (JSON):\n" + record_json
    msgs = []
    for m in (body.messages or [])[-24:]:
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            msgs.append({"role": role, "content": content})
    # Anthropic requires the conversation to start with a user turn.
    if not msgs or msgs[0]["role"] != "user":
        msgs = [{"role": "user", "content": "Study this case and give your clinical impression."}] + msgs
    client = _client()
    try:
        resp = client.messages.create(model=MODEL, max_tokens=3000, system=system, messages=msgs)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AI request failed: {e}")
    text = "".join(b.text for b in (resp.content or []) if getattr(b, "type", None) == "text").strip()
    return {"reply": text}


def _build_chat(body: "ChatIn"):
    """Shared request-shaping for both the blocking and streaming chat endpoints."""
    record_json = json.dumps(body.record, ensure_ascii=False, indent=2)
    system = CHAT_SYSTEM + "\n\nDe-identified patient record (JSON):\n" + record_json
    msgs = []
    for m in (body.messages or [])[-24:]:
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            msgs.append({"role": role, "content": content})
    if not msgs or msgs[0]["role"] != "user":
        msgs = [{"role": "user", "content": "Study this case and give your clinical impression."}] + msgs
    return system, msgs


@router.post("/api/ai/chat/stream")
def chat_stream(body: ChatIn):
    """Same as /chat but streams the answer token-by-token (text/plain), so the reply
    appears as it's written instead of after a long wait. The client appends each chunk."""
    system, msgs = _build_chat(body)
    client = _client()

    def gen():
        try:
            with client.messages.stream(model=MODEL, max_tokens=3000, system=system, messages=msgs) as stream:
                for chunk in stream.text_stream:
                    if chunk:
                        yield chunk
        except Exception as e:  # surface the error inside the stream so the UI can show it
            yield f"\n\n(AI request failed: {e})"

    return StreamingResponse(
        gen(),
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
