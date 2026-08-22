"""Phase 4 — three-way benchmark: A local, B frontier-unredacted (ceiling),
C sandwich. 15 tasks x 3 modes, blind pairwise judging via Anthropic,
leak grep of every C outbound payload against the manifest.

Run: (env from stack .env) /workspace/venvs/pipeline/bin/python benchmark.py
Writes /workspace/audit/benchmark.json
"""

import json
import os
import re
import time
from pathlib import Path

import httpx

PIPE = "http://127.0.0.1:8092"
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CORPUS = Path("/workspace/corpus")

manifest = json.loads((CORPUS / "ENTITY_MANIFEST.json").read_text())
idx = json.loads((CORPUS / "corpus_index.json").read_text())

# pick real entities so grounded lookups have real answers
cli_docs = {f.stem: f.read_text() for f in (CORPUS / "docs").glob("CLI-*.txt")}
some_clients = [re.search(r"Client: (.+)", t).group(1)
                for t in list(cli_docs.values())[:5]]
plc_docs = {f.stem: f.read_text() for f in (CORPUS / "docs").glob("PLC-*.txt")}
some_cands = [re.search(r"Candidate: (.+?) \(", t).group(1)
              for t in list(plc_docs.values())[:5]]

TASKS = (
    [{"tier": "lookup", "q": f"What placement fee percentage and rebate terms "
      f"did we agree with {c}?"} for c in some_clients]
    + [{"tier": "synthesis", "q": q} for q in [
        "Draft a fee-increase strategy memo for the Atlas Global Bank managed "
        "account, grounded in our placement history with them.",
        f"Summarize our placement history with {some_cands[0]} and recommend "
        "next roles to pitch.",
        "Write a one-page brief comparing our fee terms across banking clients "
        "and where we are underpriced.",
        f"Draft an outreach plan to re-engage {some_cands[1]} for a senior "
        "compliance role.",
        "Prepare talking points for renewing the Atlas Global Bank terms of "
        "business next quarter."]]
    + [{"tier": "reasoning", "q": q} for q in [
        "Restructure our rebate policy given our placement economics: model "
        "the exposure and propose thresholds.",
        "Our anchor account concentration looks risky. Quantify it from the "
        "placements and propose a de-risking plan.",
        "Propose a tiered fee structure that would raise blended fees by 2 "
        "points without losing the top three clients by volume.",
        "Which client relationships show declining economics, and what "
        "specific renegotiation strategy should we run for each?",
        "Design a quarterly account-health scorecard using only metrics we "
        "can compute from our own placement records."]])

def ask(q, mode):
    t0 = time.time()
    r = httpx.post(f"{PIPE}/ask", json={"question": q, "mode": mode},
                   timeout=1800)
    r.raise_for_status()
    d = r.json()
    d["latency"] = round(time.time() - t0, 1)
    return d

def judge(question, ans_x, ans_y):
    """Blind pairwise: returns 'X', 'Y' or 'TIE'."""
    prompt = (f"Question posed to a recruitment-firm assistant:\n{question}\n\n"
              f"ANSWER X:\n{ans_x[:4000]}\n\nANSWER Y:\n{ans_y[:4000]}\n\n"
              "Judge on correctness, groundedness (specific, plausible, "
              "internally consistent figures), and completeness. Reply with "
              "exactly one token: X, Y, or TIE.")
    r = httpx.post("https://api.anthropic.com/v1/messages",
                   headers={"x-api-key": ANTHROPIC_KEY,
                            "anthropic-version": "2023-06-01"},
                   json={"model": "claude-sonnet-4-6", "max_tokens": 5,
                         "messages": [{"role": "user", "content": prompt}]},
                   timeout=120)
    r.raise_for_status()
    v = r.json()["content"][0]["text"].strip().upper()
    return v if v in ("X", "Y", "TIE") else "TIE"

def main():
    runs = []
    for i, task in enumerate(TASKS):
        row = {"task": i, "tier": task["tier"], "q": task["q"], "runs": {}}
        for mode, label in [("local", "A"), ("frontier-unredacted", "B"),
                            ("sandwich", "C")]:
            try:
                d = ask(task["q"], mode)
                row["runs"][label] = {
                    "answer": d["answer"], "latency": d["latency"],
                    "gate": d.get("gate", {}).get("status") if d.get("gate") else None,
                    "provider": d.get("frontier_provider")}
            except Exception as e:
                row["runs"][label] = {"error": str(e)[:200]}
            print(f"task {i} {label}: "
                  f"{row['runs'][label].get('latency', 'ERR')}s", flush=True)
        runs.append(row)

    # blind pairwise judging with randomized position (seeded)
    import random
    random.seed(7)
    for row in runs:
        r = row["runs"]
        for pair in (("C", "A"), ("C", "B")):
            a, b = pair
            if "answer" not in r.get(a, {}) or "answer" not in r.get(b, {}):
                row[f"judge_{a}v{b}"] = "N/A"
                continue
            flip = random.random() < 0.5
            x, y = (b, a) if flip else (a, b)
            v = judge(row["q"], r[x]["answer"], r[y]["answer"])
            winner = {"X": x, "Y": y, "TIE": "TIE"}[v]
            row[f"judge_{a}v{b}"] = winner
            time.sleep(1)

    # leak grep: every C outbound payload vs manifest
    entities = sorted({e for k, v in manifest.items()
                       if not k.startswith("_") for e in v}, key=len, reverse=True)
    leaks = []
    for line in open("/workspace/traces/pipeline.jsonl"):
        d = json.loads(line)
        if d["mode"] != "sandwich":
            continue
        for hop in d["hops"]:
            if hop.get("hop") == "frontier" and "outbound_payload" in hop:
                blob = json.dumps(hop["outbound_payload"], ensure_ascii=False)
                for e in entities:
                    if e in blob:
                        leaks.append({"session": d["session_id"], "entity": e})
    result = {"tasks": len(TASKS), "runs": runs, "outbound_leaks": leaks,
              "leak_count": len(leaks)}

    # tabulate
    tiers = {}
    for row in runs:
        t = tiers.setdefault(row["tier"], {"CvA": [], "CvB": []})
        t["CvA"].append(row.get("judge_CvA"))
        t["CvB"].append(row.get("judge_CvB"))
    result["summary"] = tiers
    Path("/workspace/audit/benchmark.json").write_text(
        json.dumps(result, indent=1, ensure_ascii=False))
    print(json.dumps({"summary": tiers, "leak_count": len(leaks)}, indent=1))

if __name__ == "__main__":
    main()
