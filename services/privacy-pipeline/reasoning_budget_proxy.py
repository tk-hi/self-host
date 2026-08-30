"""reasoning_budget_proxy.py — bound qwen3's <think> so it always answers.

Problem: on open-ended prompts qwen3.8 fills the whole output budget with
reasoning inside <think>…</think> and never emits an answer → Onyx shows
"Response was terminated prior to completion" (or hangs for minutes).

Fix: "budget forcing". Render the prompt with the model's own chat template
(so system/history are correct), then:
  Phase 1  reason with max_tokens=THINK_BUDGET, stop at </think>
  Phase 2  force-close the </think> and generate the answer (streamed)
Reasoning is capped; an answer is always produced; total ≤ THINK+ANSWER.

Requests that carry `tools`/`functions` are passed straight through to raw vLLM
chat (budget-forcing via the completions API can't emit tool-calls).

OpenAI-compatible: GET /health, GET /v1/models, POST /v1/chat/completions.
Runs in the vLLM venv (needs transformers for the tokenizer). Loopback-only.
"""
import json
import os
import time
import uuid
import urllib.request
import urllib.error

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_HOME", "/workspace/hf-cache")

from fastapi import FastAPI
from fastapi.responses import StreamingResponse, JSONResponse
from transformers import AutoTokenizer

VLLM_BASE = os.environ.get("VLLM_BASE", "http://127.0.0.1:8000/v1")
VLLM_COMPLETIONS = VLLM_BASE + "/completions"
VLLM_CHAT = VLLM_BASE + "/chat/completions"
KEY = os.environ.get("VLLM_API_KEY", "")
MODEL = os.environ.get("SERVED_MODEL_NAME", "qwen3.8-27b-uncensored")
MODEL_REPO = os.environ.get("MODEL_ID", "philbert440/Qwen3.8-27B-Uncensored-Aggressive-W4A16-AWQ")
THINK_BUDGET = int(os.environ.get("THINK_BUDGET", "3000"))
ANSWER_BUDGET = int(os.environ.get("ANSWER_BUDGET", "6000"))
# Tool-bearing requests bypass budget forcing, so cap them here: without a cap
# a reasoning spiral runs to the model's context limit (~25 min of GPU).
PASSTHROUGH_MAX_TOKENS = int(os.environ.get("PASSTHROUGH_MAX_TOKENS", str(THINK_BUDGET + ANSWER_BUDGET)))
# The aggressive W4A16 quant degrades into cyclic repetition on long
# generations; a mild penalty breaks the cycle without hurting code output.
REPETITION_PENALTY = float(os.environ.get("REPETITION_PENALTY", "1.05"))
# Tool-bearing requests can't be budget-forced, and this model reasons to the
# token cap without ever answering (verified on the 2026-08-28 incident
# prompt). Disable thinking for them instead: answers and tool calls both
# arrive in seconds. Set PASSTHROUGH_THINKING=1 to re-enable.
PASSTHROUGH_THINKING = os.environ.get("PASSTHROUGH_THINKING", "0") == "1"
IM_END = "<|im_end|>"

_tok = AutoTokenizer.from_pretrained(MODEL_REPO)
app = FastAPI(title="Midas reasoning-budget proxy")


def _headers():
    return {"Authorization": "Bearer " + KEY, "Content-Type": "application/json"}


def _post(url, body, stream=False):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=_headers())
    return urllib.request.urlopen(req, timeout=600)


def _completion(prompt, max_tokens, stop, temperature):
    body = {"model": MODEL, "prompt": prompt, "max_tokens": max_tokens,
            "temperature": temperature, "stop": stop, "stream": False,
            "repetition_penalty": REPETITION_PENALTY}
    d = json.load(_post(VLLM_COMPLETIONS, body))
    c = d["choices"][0]
    return c["text"], c.get("finish_reason"), d.get("usage", {})


def _stream_completion(prompt, max_tokens, stop, temperature):
    """Yield text pieces from a streamed vLLM completion."""
    body = {"model": MODEL, "prompt": prompt, "max_tokens": max_tokens,
            "temperature": temperature, "stop": stop, "stream": True,
            "repetition_penalty": REPETITION_PENALTY}
    r = _post(VLLM_COMPLETIONS, body, stream=True)
    for line in r:
        line = line.decode(errors="ignore").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            piece = json.loads(data)["choices"][0].get("text", "")
        except Exception:
            continue
        if piece:
            yield piece


def _render(messages):
    # Uses the model's chat template (correct system/history handling). The
    # template already ends the assistant turn with "<think>\n".
    return _tok.apply_chat_template(messages, add_generation_prompt=True, tokenize=False)


def _sse(cid, created, delta, finish=None):
    return "data: " + json.dumps({
        "id": cid, "object": "chat.completion.chunk", "created": created,
        "model": MODEL, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]
    }) + "\n\n"


# ---------- OpenAI surface ----------
@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL, "think_budget": THINK_BUDGET,
            "answer_budget": ANSWER_BUDGET, "upstream": VLLM_BASE,
            "passthrough_max_tokens": PASSTHROUGH_MAX_TOKENS,
            "repetition_penalty": REPETITION_PENALTY}


@app.get("/v1/models")
def models():
    return {"object": "list", "data": [
        {"id": MODEL, "object": "model", "created": 0, "owned_by": "midas-budget"}]}


@app.post("/v1/chat/completions")
def chat_completions(body: dict):
    messages = body.get("messages") or []
    temperature = body.get("temperature", 0.7)
    want_stream = bool(body.get("stream"))

    # Tool-calling requests: pass through untouched so vLLM's tool parser runs.
    if body.get("tools") or body.get("functions"):
        return _passthrough(body, want_stream)

    try:
        base = _render(messages)
    except Exception:
        # If templating fails on an unusual message shape, don't break the chat.
        return _passthrough(body, want_stream)

    cid = "chatcmpl-" + uuid.uuid4().hex[:12]
    created = int(time.time())

    if want_stream:
        def gen():
            yield _sse(cid, created, {"role": "assistant"})
            # Phase 1: STREAM the reasoning (as reasoning_content) so bytes flow
            # from the first second — no dead air — and the client shows "thinking".
            parts = []
            for piece in _stream_completion(base, THINK_BUDGET, ["</think>"], temperature):
                parts.append(piece)
                yield _sse(cid, created, {"reasoning_content": piece})
            # Phase 2: force </think> closed and STREAM the answer.
            answer_prompt = base + "".join(parts) + "\n</think>\n\n"
            for piece in _stream_completion(answer_prompt, ANSWER_BUDGET, [IM_END], temperature):
                yield _sse(cid, created, {"content": piece})
            yield _sse(cid, created, {}, finish="stop")
            yield "data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    # Non-streaming: bounded reasoning, then forced answer.
    reasoning, _fr, _u = _completion(base, THINK_BUDGET, ["</think>"], temperature)
    answer_prompt = base + reasoning + "\n</think>\n\n"
    answer, _fr2, usage = _completion(answer_prompt, ANSWER_BUDGET, [IM_END], temperature)
    return {
        "id": cid, "object": "chat.completion", "created": created, "model": MODEL,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": answer.strip()}}],
        "usage": usage or {},
    }


def _passthrough(body, want_stream):
    # vLLM only knows the real served name — never our display-name model id.
    body = {**body, "model": MODEL}
    requested = body.get("max_tokens") or body.get("max_completion_tokens")
    body["max_tokens"] = min(requested or PASSTHROUGH_MAX_TOKENS, PASSTHROUGH_MAX_TOKENS)
    body.pop("max_completion_tokens", None)
    body.setdefault("repetition_penalty", REPETITION_PENALTY)
    if not PASSTHROUGH_THINKING:
        body.setdefault("chat_template_kwargs", {"enable_thinking": False})
    try:
        r = _post(VLLM_CHAT, body, stream=want_stream)
    except urllib.error.HTTPError as e:
        return JSONResponse(status_code=e.code, content=json.loads(e.read() or b"{}"))
    if want_stream:
        def relay():
            for line in r:
                yield line
        return StreamingResponse(relay(), media_type="text/event-stream")
    return JSONResponse(content=json.load(r))
