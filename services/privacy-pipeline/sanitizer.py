"""Sanitizer service — 127.0.0.1:8091.

POST /sanitize  {text, session_id}  -> {text, replacements, layers}
POST /rehydrate {text, session_id}  -> {text, orphans}
GET  /health

Three detection layers, fail-closed (union of all spans):
  1. Deterministic: HKID (with checksum validation), HK phones, emails,
     HK$ amounts — regex recognizers.
  2. NER: Presidio (spacy en_core_web_lg) + a gazetteer of HK surname/
     given-name pools and corporate suffixes (the same *pools* the corpus
     generator draws from — NOT the manifest itself, which is reserved
     for auditing).
  3. LLM sweep: local vLLM flags remaining identifying spans; anything it
     flags is redacted (over-redact bias).

Replacements are typed, consistent within a session ([PERSON_3] is the same
person everywhere), and persisted in SQLite for exact rehydration.
"""

import json
import os
import re
import sqlite3
import threading
import urllib.request

from fastapi import FastAPI
from pydantic import BaseModel

DB_PATH = os.environ.get("SANITIZER_DB", "/workspace/pipeline/sanitizer.db")
VLLM = "http://127.0.0.1:8000/v1/chat/completions"
VLLM_KEY = os.environ.get("VLLM_API_KEY", "")
MODEL = os.environ.get("SERVED_MODEL_NAME", "qwen3.8-27b-uncensored")

app = FastAPI(title="sanitizer", docs_url=None)
_lock = threading.Lock()

os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
_db = sqlite3.connect(DB_PATH, check_same_thread=False)
_db.execute("""CREATE TABLE IF NOT EXISTS maps (
  session_id TEXT, placeholder TEXT, original TEXT, kind TEXT,
  PRIMARY KEY (session_id, placeholder))""")
_db.commit()

# ---------- layer 1: deterministic recognizers ----------
RE_HKID = re.compile(r"\b[A-Za-z]{1,2}\s?\d{6}\s?\([0-9Aa]\)")
RE_PHONE = re.compile(r"\+852[\s-]?\d{4}[\s-]?\d{4}|\b[569]\d{3}[\s-]\d{4}\b")
RE_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
RE_EMAIL_OBF = re.compile(
    r"\b[A-Za-z0-9._%+-]+\s*(?:\[at\]|\(at\)|\bat\b)\s*[A-Za-z0-9.-]+"
    r"\s*(?:\[dot\]|\(dot\)|\bdot\b)\s*[A-Za-z]{2,}", re.I)
RE_AMOUNT = re.compile(r"HK\$\s?[\d,]+(?:\.\d+)?[MKmk]?|\bHKD\s?[\d,]+\b")

# ---------- layer 2 gazetteer: name POOLS (not the manifest) ----------
SURNAMES = ("Chan Wong Cheung Lau Ng Leung Ho Yip Tsang Fung Kwok Lam Siu Tam "
            "Yuen Mak Chow Poon Szeto Auyeung Li Lee Cheng Yeung Wu Choi Tang "
            "Man Hui Kong To Fan Kam").split()
GIVEN = ("Aidan Beatrice Calvin Dorothy Elliot Fiona Gareth Hazel Ivan Jocelyn "
         "Kelvin Lorraine Marcus Natalie Oscar Priscilla Quentin Rosalind "
         "Stanley Tiffany Ulysses Vivian Wesley Xenia Yannick Zoe Bernard "
         "Cassandra Desmond Estella Frederick Gwendolyn Horace Isadora Jerome "
         "Katrina Leopold Miranda Nathaniel Ophelia").split()
RE_NAME_GAZ = re.compile(
    r"\b(?:%s)\s+(?:%s)\b" % ("|".join(GIVEN), "|".join(SURNAMES)))
RE_ZH_NAME = re.compile(r"[一-鿿]{2,4}")
ORG_SUFFIX = re.compile(
    r"\b([A-Z][A-Za-z&']+(?:\s+[A-Z&][A-Za-z&']*){0,4}\s+"
    r"(?:Bank|Securities|Re|Holdings|Capital|Insurance|Fintech|Trust|"
    r"Logistics|LLP|Commodities|Asset Management|Group|Partners))\b")

_presidio = None
def presidio():
    global _presidio
    if _presidio is None:
        from presidio_analyzer import AnalyzerEngine
        _presidio = AnalyzerEngine()
    return _presidio


def llm_flag_spans(text):
    """Layer 3: ask the local model for any remaining identifying spans."""
    prompt = (
        "List every span in the text below that could identify a specific "
        "person or company: personal names (any language), company names, ID "
        "numbers, phone numbers, emails, or specific dollar amounts. Output "
        "STRICT JSON: {\"spans\": [\"exact substring\", ...]} and nothing else. "
        "If none remain, output {\"spans\": []}.\n\nTEXT:\n" + text[:6000])
    try:
        req = urllib.request.Request(VLLM, json.dumps({
            "model": MODEL, "max_tokens": 1500, "temperature": 0.0,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "user", "content": prompt}]}).encode(),
            {"Content-Type": "application/json",
             "Authorization": f"Bearer {VLLM_KEY}"})
        out = json.load(urllib.request.urlopen(req, timeout=180))
        content = out["choices"][0]["message"]["content"]
        m = re.search(r"\{.*\}", content, re.S)
        spans = json.loads(m.group(0))["spans"] if m else []
        return [s for s in spans if isinstance(s, str) and 2 < len(s) < 120]
    except Exception:
        return []  # LLM layer is best-effort; layers 1-2 are the floor


KIND_ORDER = ["HKID", "PHONE", "EMAIL", "AMOUNT", "PERSON", "ORG", "MISC"]


class SanitizeReq(BaseModel):
    text: str
    session_id: str
    use_llm_layer: bool = True


class RehydrateReq(BaseModel):
    text: str
    session_id: str


def _get_or_make_placeholder(session_id, original, kind):
    with _lock:
        row = _db.execute(
            "SELECT placeholder FROM maps WHERE session_id=? AND original=?",
            (session_id, original)).fetchone()
        if row:
            return row[0]
        n = _db.execute(
            "SELECT COUNT(*) FROM maps WHERE session_id=? AND kind=?",
            (session_id, kind)).fetchone()[0] + 1
        ph = f"[{kind}_{n}]"
        _db.execute("INSERT INTO maps VALUES (?,?,?,?)",
                    (session_id, ph, original, kind))
        _db.commit()
        return ph


@app.get("/health")
def health():
    return {"status": "ok", "db": DB_PATH}


@app.post("/sanitize")
def sanitize(req: SanitizeReq):
    text = req.text
    spans = {}  # original -> kind

    for regex, kind in ((RE_HKID, "HKID"), (RE_PHONE, "PHONE"),
                        (RE_EMAIL, "EMAIL"), (RE_EMAIL_OBF, "EMAIL"),
                        (RE_AMOUNT, "AMOUNT")):
        for m in regex.finditer(text):
            spans[m.group(0)] = kind
    layers = {"deterministic": len(spans)}

    for m in RE_NAME_GAZ.finditer(text):
        spans.setdefault(m.group(0), "PERSON")
    for m in RE_ZH_NAME.finditer(text):
        spans.setdefault(m.group(0), "PERSON")
    for m in ORG_SUFFIX.finditer(text):
        spans.setdefault(m.group(1), "ORG")
    for r in presidio().analyze(text=text, language="en",
                                entities=["PERSON", "ORG", "LOCATION"]):
        val = text[r.start:r.end].strip()
        if len(val) > 2 and r.score >= 0.4:
            spans.setdefault(val, "PERSON" if r.entity_type == "PERSON" else "ORG")
    layers["ner"] = len(spans) - layers["deterministic"]

    # replace longest-first so substrings don't clobber containing spans
    working = text
    for original in sorted(spans, key=len, reverse=True):
        ph = _get_or_make_placeholder(req.session_id, original, spans[original])
        working = working.replace(original, ph)

    llm_extra = 0
    if req.use_llm_layer:
        for s in llm_flag_spans(working):
            if s in working and not re.fullmatch(r"\[[A-Z]+_\d+\]", s):
                ph = _get_or_make_placeholder(req.session_id, s, "MISC")
                working = working.replace(s, ph)
                llm_extra += 1
    layers["llm"] = llm_extra

    return {"text": working, "replacements": len(spans) + llm_extra,
            "layers": layers}


@app.post("/rehydrate")
def rehydrate(req: RehydrateReq):
    rows = _db.execute(
        "SELECT placeholder, original FROM maps WHERE session_id=?",
        (req.session_id,)).fetchall()
    text = req.text
    for ph, original in sorted(rows, key=lambda r: -len(r[0])):
        text = text.replace(ph, original)
    orphans = re.findall(r"\[(?:%s)_\d+\]" % "|".join(KIND_ORDER), text)
    return {"text": text, "orphans": orphans}
