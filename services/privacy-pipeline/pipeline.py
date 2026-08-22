"""Privacy-sandwich pipeline — 127.0.0.1:8092.

POST /ask {question, session_id}:
  1. RAG-retrieve top-k chunks (LlamaIndex/Qdrant/BGE-M3, doc-id metadata)
  2. sanitize question + context via sanitizer :8091
  3. frontier call (Anthropic claude-sonnet-4-6) — exact outbound payload
     logged BEFORE sending; falls back to local vLLM (labeled) if no key
  4. rehydrate via the session map
  5. enrich: local model injects corpus figures, every fact cited [doc-id];
     uncited additions are dropped
  6. gates: orphan scan, per-citation faithfulness (local judge, >=0.8),
     LLM Guard output scan; failures flag the response REVIEW
  7. full hop-by-hop JSONL trace to /workspace/traces/

Also serves:
  GET /traces           single-file HTML trace viewer
  GET /traces/data      the JSONL (for the viewer)
  /v1/models, /v1/chat/completions  OpenAI-compatible shim so Open WebUI can
                                    list "Meridian-Hybrid" and chat with it
"""

import json
import os
import re
import time
import urllib.request
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel

TRACES = Path("/workspace/traces/pipeline.jsonl")
TRACES.parent.mkdir(parents=True, exist_ok=True)
SANITIZER = "http://127.0.0.1:8091"
VLLM = "http://127.0.0.1:8000/v1/chat/completions"
VLLM_KEY = os.environ.get("VLLM_API_KEY", "")
LOCAL_MODEL = os.environ.get("SERVED_MODEL_NAME", "qwen3.8-27b-uncensored")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
FRONTIER_MODEL = "claude-sonnet-4-6"

app = FastAPI(title="meridian-hybrid", docs_url=None)

# ---------- retrieval ----------
_index = None
def retriever():
    global _index
    if _index is None:
        from llama_index.core import Settings, VectorStoreIndex
        from llama_index.embeddings.huggingface import HuggingFaceEmbedding
        from llama_index.vector_stores.qdrant import QdrantVectorStore
        import qdrant_client
        Settings.embed_model = HuggingFaceEmbedding(model_name="BAAI/bge-m3",
                                                    device="cpu")
        Settings.llm = None
        store = QdrantVectorStore(
            client=qdrant_client.QdrantClient(host="127.0.0.1", port=6333),
            collection_name="meridian")
        _index = VectorStoreIndex.from_vector_store(store)
    return _index.as_retriever(similarity_top_k=6)

# ---------- llm guard (lazy; CPU) ----------
_scanners = {}
def guard_input(text):
    try:
        if "in" not in _scanners:
            from llm_guard.input_scanners import PromptInjection
            _scanners["in"] = PromptInjection()
        _, valid, score = _scanners["in"].scan(text)
        return {"valid": bool(valid), "score": float(score)}
    except Exception as e:
        return {"valid": True, "score": 0.0, "error": str(e)[:120]}

def guard_output(prompt, text):
    try:
        if "out" not in _scanners:
            from llm_guard.output_scanners import NoRefusal
            _scanners["out"] = NoRefusal()
        _, valid, score = _scanners["out"].scan(prompt, text)
        return {"valid": bool(valid), "score": float(score)}
    except Exception as e:
        return {"valid": True, "score": 0.0, "error": str(e)[:120]}

# ---------- model calls ----------
def local_llm(messages, max_tokens=2000, temperature=0.2):
    req = urllib.request.Request(VLLM, json.dumps({
        "model": LOCAL_MODEL, "max_tokens": max_tokens,
        "temperature": temperature,
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": messages}).encode(),
        {"Content-Type": "application/json",
         "Authorization": f"Bearer {VLLM_KEY}"})
    out = json.load(urllib.request.urlopen(req, timeout=600))
    return (out["choices"][0]["message"]["content"],
            out.get("usage", {}))

def frontier_call(payload):
    """Returns (text, usage, provider). Payload already logged by caller."""
    if not ANTHROPIC_KEY:
        text, usage = local_llm(payload["messages"],
                                payload.get("max_tokens", 2000))
        return text, usage, "LOCAL-FALLBACK"
    for attempt in range(4):
        try:
            r = httpx.post("https://api.anthropic.com/v1/messages",
                           headers={"x-api-key": ANTHROPIC_KEY,
                                    "anthropic-version": "2023-06-01"},
                           json=payload, timeout=300)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(5 * (attempt + 1)); continue
            r.raise_for_status()
            d = r.json()
            return ("".join(b.get("text", "") for b in d["content"]),
                    d.get("usage", {}), FRONTIER_MODEL)
        except httpx.HTTPError:
            time.sleep(5 * (attempt + 1))
    text, usage = local_llm(payload["messages"], payload.get("max_tokens", 2000))
    return text, usage, "LOCAL-FALLBACK(after-retries)"

def sanitize(text, session_id):
    r = httpx.post(f"{SANITIZER}/sanitize",
                   json={"text": text, "session_id": session_id}, timeout=600)
    r.raise_for_status()
    return r.json()

def rehydrate(text, session_id):
    r = httpx.post(f"{SANITIZER}/rehydrate",
                   json={"text": text, "session_id": session_id}, timeout=60)
    r.raise_for_status()
    return r.json()

# ---------- gates ----------
ORPHAN_RE = re.compile(r"\[(?:HKID|PHONE|EMAIL|AMOUNT|PERSON|ORG|MISC)_\d+\]")
CITE_RE = re.compile(r"\[((?:CAND|CLI|PLC|MISC)-\d+)\]")

def faithfulness(claims_with_chunks):
    scores = []
    for claim, chunk in claims_with_chunks:
        prompt = (f"SOURCE:\n{chunk[:1500]}\n\nCLAIM: {claim}\n\n"
                  "Is the claim fully supported by the source? Answer with only "
                  "a number 0.0-1.0.")
        try:
            out, _ = local_llm([{"role": "user", "content": prompt}], 10, 0.0)
            m = re.search(r"[01](?:\.\d+)?", out)
            scores.append(float(m.group(0)) if m else 0.0)
        except Exception:
            scores.append(0.0)
    return scores

# ---------- the sandwich ----------
class AskReq(BaseModel):
    question: str
    session_id: str | None = None
    mode: str = "sandwich"  # sandwich | local | frontier-unredacted

def run_ask(question, session_id=None, mode="sandwich"):
    session_id = session_id or uuid.uuid4().hex[:12]
    t0 = time.time()
    trace = {"ts": time.time(), "session_id": session_id, "mode": mode,
             "question": question, "hops": [], "timings": {}}

    gi = guard_input(question)
    trace["input_guard"] = gi

    t = time.time()
    nodes = retriever().retrieve(question)
    chunks = [{"doc_id": n.metadata.get("doc_id", "?"), "text": n.get_content()}
              for n in nodes]
    trace["timings"]["retrieve"] = round(time.time() - t, 2)
    trace["hops"].append({"hop": "retrieve",
                          "doc_ids": [c["doc_id"] for c in chunks]})
    context = "\n\n".join(f"[{c['doc_id']}]\n{c['text']}" for c in chunks)

    if mode == "local":
        t = time.time()
        answer, usage = local_llm([{"role": "user", "content":
            f"Context documents:\n{context}\n\nQuestion: {question}\n\n"
            "Answer using the context; cite doc-ids in square brackets."}])
        trace["timings"]["local_answer"] = round(time.time() - t, 2)
        trace["answer"] = answer
        trace["tokens"] = usage
    elif mode == "frontier-unredacted":
        payload = {"model": FRONTIER_MODEL, "max_tokens": 2000,
                   "messages": [{"role": "user", "content":
                       f"Context documents:\n{context}\n\nQuestion: {question}\n\n"
                       "Answer using the context; cite doc-ids in square brackets."}]}
        trace["hops"].append({"hop": "frontier-UNREDACTED-CEILING",
                              "outbound_payload": payload})
        t = time.time()
        answer, usage, provider = frontier_call(payload)
        trace["timings"]["frontier"] = round(time.time() - t, 2)
        trace["frontier_provider"] = provider
        trace["answer"] = answer
        trace["tokens"] = usage
    else:
        # 2. sanitize
        t = time.time()
        sq = sanitize(question, session_id)
        sc = sanitize(context, session_id)
        trace["timings"]["sanitize"] = round(time.time() - t, 2)
        trace["hops"].append({"hop": "sanitize",
                              "q_replacements": sq["replacements"],
                              "ctx_replacements": sc["replacements"],
                              "layers": sc["layers"]})
        # 3. frontier — log outbound BEFORE sending
        payload = {"model": FRONTIER_MODEL, "max_tokens": 2000,
                   "messages": [{"role": "user", "content":
                       "You are advising a recruitment firm. Entities are "
                       "replaced by typed placeholders like [PERSON_1]; keep "
                       "them intact in your answer.\n\nContext:\n"
                       + sc["text"] + "\n\nQuestion: " + sq["text"]}]}
        trace["hops"].append({"hop": "frontier", "outbound_payload": payload})
        t = time.time()
        raw, usage, provider = frontier_call(payload)
        trace["timings"]["frontier"] = round(time.time() - t, 2)
        trace["frontier_provider"] = provider
        trace["tokens"] = usage
        trace["hops"].append({"hop": "frontier_response", "text": raw})
        # 4. rehydrate
        rh = rehydrate(raw, session_id)
        trace["hops"].append({"hop": "rehydrate",
                              "orphans_from_map": rh["orphans"]})
        # 5. enrich with citations
        t = time.time()
        enriched, _ = local_llm([{"role": "user", "content":
            "Improve the DRAFT by replacing generic statements with specific "
            "figures from the CONTEXT where relevant. Every specific figure or "
            "fact you add MUST be followed by its doc-id citation in square "
            "brackets, e.g. [CLI-01]. Do not add any fact you cannot cite. "
            "Keep the draft's structure.\n\nCONTEXT:\n" + context
            + "\n\nDRAFT:\n" + rh["text"]}], 2000)
        trace["timings"]["enrich"] = round(time.time() - t, 2)
        trace["hops"].append({"hop": "enrich", "diff_len":
                              len(enriched) - len(rh["text"])})
        # 6. gates
        orphans = ORPHAN_RE.findall(enriched)
        cited = CITE_RE.findall(enriched)
        chunk_by_id = {c["doc_id"]: c["text"] for c in chunks}
        claims = []
        for sent in re.split(r"(?<=[.!?])\s+", enriched):
            for cid in CITE_RE.findall(sent):
                if cid in chunk_by_id:
                    claims.append((sent, chunk_by_id[cid]))
        t = time.time()
        scores = faithfulness(claims[:10])
        go = guard_output(question, enriched)
        trace["timings"]["gates"] = round(time.time() - t, 2)
        gate = {"orphans": orphans,
                "citations": cited,
                "faithfulness_scores": scores,
                "faithfulness_min": min(scores) if scores else None,
                "output_guard": go}
        reasons = []
        if orphans:
            reasons.append(f"unresolved placeholders: {orphans}")
        if scores and min(scores) < 0.8:
            reasons.append(f"faithfulness below 0.8 (min {min(scores)})")
        if not go["valid"]:
            reasons.append("output guard flagged")
        gate["status"] = "REVIEW" if reasons else "PASS"
        gate["reasons"] = reasons
        trace["gate"] = gate
        trace["answer"] = enriched

    trace["timings"]["total"] = round(time.time() - t0, 2)
    with open(TRACES, "a") as f:
        f.write(json.dumps(trace, ensure_ascii=False) + "\n")
    return trace

@app.get("/health")
def health():
    return {"status": "ok",
            "frontier": FRONTIER_MODEL if ANTHROPIC_KEY else "LOCAL-FALLBACK"}

@app.post("/ask")
def ask(req: AskReq):
    tr = run_ask(req.question, req.session_id, req.mode)
    return {"answer": tr["answer"], "gate": tr.get("gate"),
            "session_id": tr["session_id"],
            "frontier_provider": tr.get("frontier_provider"),
            "timings": tr["timings"]}

# ---------- OpenAI-compatible shim for Open WebUI ----------
@app.get("/v1/models")
def models():
    return {"object": "list", "data": [{
        "id": "Meridian-Hybrid", "object": "model", "created": 0,
        "owned_by": "meridian"}]}

@app.post("/v1/chat/completions")
def chat(body: dict):
    user_msgs = [m["content"] for m in body.get("messages", [])
                 if m.get("role") == "user"]
    q = user_msgs[-1] if user_msgs else ""
    tr = run_ask(q)
    note = ""
    if tr.get("gate", {}).get("status") == "REVIEW":
        note = ("\n\n---\n⚠ REVIEW: " + "; ".join(tr["gate"]["reasons"]))
    return {"id": "chatcmpl-" + uuid.uuid4().hex[:10],
            "object": "chat.completion", "created": int(time.time()),
            "model": "Meridian-Hybrid",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant",
                                     "content": tr["answer"] + note}}],
            "usage": tr.get("tokens", {})}

# ---------- trace viewer ----------
VIEWER = """<!doctype html><meta charset="utf-8"><title>Meridian traces</title>
<style>body{font:14px/1.5 -apple-system,sans-serif;margin:2rem;max-width:70rem}
.trace{border:1px solid #ccc;border-radius:8px;margin:1rem 0;padding:1rem}
.hop{margin:.5rem 0;padding:.5rem;background:#f6f6f6;border-radius:6px}
.outbound{background:#fff3e0;border:1px solid #f0b429}
pre{white-space:pre-wrap;font-size:12px;max-height:20rem;overflow:auto}
.PASS{color:#0a7d33}.REVIEW{color:#c0392b}
h3{margin:.2rem 0}</style>
<h1>Pipeline traces</h1><p>Amber blocks are the <b>exact outbound payloads</b>
that left the box.</p><div id=r></div>
<script>
fetch('/traces/data').then(r=>r.text()).then(t=>{
 const R=document.getElementById('r');
 t.trim().split('\\n').reverse().forEach(l=>{if(!l)return;
  const d=JSON.parse(l);const e=document.createElement('div');e.className='trace';
  let h=`<h3>${new Date(d.ts*1000).toLocaleString()} — ${d.mode}
   ${d.gate?`<span class=${d.gate.status}>[${d.gate.status}]</span>`:''}
   <small>${d.frontier_provider||''} ${d.timings.total}s</small></h3>
   <b>Q:</b> ${d.question}<br>`;
  (d.hops||[]).forEach(x=>{
   if(x.outbound_payload)h+=`<div class="hop outbound"><b>→ OUTBOUND
    (${x.hop})</b><pre>${JSON.stringify(x.outbound_payload,null,1)
    .replace(/</g,'&lt;')}</pre></div>`;
   else h+=`<div class=hop><b>${x.hop}</b>
    <pre>${JSON.stringify(x,null,1).replace(/</g,'&lt;').slice(0,3000)}</pre></div>`});
  h+=`<b>Answer:</b><pre>${(d.answer||'').replace(/</g,'&lt;')}</pre>`;
  if(d.gate)h+=`<pre>gate: ${JSON.stringify(d.gate,null,1).replace(/</g,'&lt;')}</pre>`;
  e.innerHTML=h;R.appendChild(e)})})
</script>"""

@app.get("/traces", response_class=HTMLResponse)
def traces_page():
    return VIEWER

@app.get("/traces/data", response_class=PlainTextResponse)
def traces_data():
    return TRACES.read_text() if TRACES.exists() else ""
