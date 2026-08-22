"""Phase 2 property test — run on the instance against the live sanitizer.

For 30 held-out docs (not used to tune anything):
  - sanitize -> deterministic grep of output against EVERY manifest entity
    (the leak test; must be ZERO hits)
  - rehydrate(sanitize(x)) -> zero orphan placeholders, and every entity
    that was replaced comes back
  - false-positive rate: tokens redacted that are NOT manifest entities
Writes /workspace/audit/sanitizer-property-test.json
"""

import json
import random
import re
import sys
from pathlib import Path

import httpx

CORPUS = Path("/workspace/corpus")
SAN = "http://127.0.0.1:8091"

manifest = json.loads((CORPUS / "ENTITY_MANIFEST.json").read_text())
entities = sorted(
    {e for k, v in manifest.items() if not k.startswith("_") for e in v},
    key=len, reverse=True)

docs = sorted((CORPUS / "docs").glob("*.txt"))
random.seed(42)
held_out = random.sample(docs, 30)

results = {"docs": [], "leaks_total": 0, "orphans_total": 0,
           "fp_redactions": 0, "total_redactions": 0}

for i, doc in enumerate(held_out):
    text = doc.read_text()
    sid = f"proptest-{i}"
    s = httpx.post(f"{SAN}/sanitize",
                   json={"text": text, "session_id": sid},
                   timeout=600).json()
    leaks = [e for e in entities if e in s["text"]]
    r = httpx.post(f"{SAN}/rehydrate",
                   json={"text": s["text"], "session_id": sid},
                   timeout=60).json()
    # every manifest entity present in the original must be back after rehydrate
    missing_after_rehydrate = [e for e in entities
                               if e in text and e not in r["text"]]
    results["docs"].append({
        "doc": doc.stem, "replacements": s["replacements"],
        "leaks": leaks, "orphans": r["orphans"],
        "missing_after_rehydrate": missing_after_rehydrate})
    results["leaks_total"] += len(leaks)
    results["orphans_total"] += len(r["orphans"]) + len(missing_after_rehydrate)
    results["total_redactions"] += s["replacements"]

# false positives: redactions beyond the count of manifest entities in the doc
for d, doc in zip(results["docs"], held_out):
    text = doc.read_text()
    true_present = len({e for e in entities if e in text})
    d["true_entities_present"] = true_present
    d["fp_estimate"] = max(0, d["replacements"] - true_present)
    results["fp_redactions"] += d["fp_estimate"]

results["fp_rate"] = round(
    results["fp_redactions"] / max(1, results["total_redactions"]), 3)
results["PASS"] = (results["leaks_total"] == 0
                   and results["orphans_total"] == 0)

Path("/workspace/audit").mkdir(exist_ok=True)
Path("/workspace/audit/sanitizer-property-test.json").write_text(
    json.dumps(results, indent=1, ensure_ascii=False))
print(json.dumps({k: v for k, v in results.items() if k != "docs"}, indent=1))
sys.exit(0 if results["PASS"] else 1)
