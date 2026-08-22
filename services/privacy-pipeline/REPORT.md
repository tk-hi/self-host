# Overnight Build Report — Privacy-Sandwich Pipeline ("Meridian-Hybrid")

Built and benchmarked 2026-08-23 (overnight, autonomous) on the vast.ai RTX
4090 instance. Every phase was verified by an independent auditor subagent
that re-ran all checks against the live system; audit files are on the
instance at `/workspace/audit/phase-{0..4}.md` and this repo carries one
commit per phase gate.

## What was built

`question → RAG (local) → sanitize (local, 3 layers) → frontier reasoning
(claude-sonnet-4-6) → re-hydrate (local) → enrich with cited corpus figures
(local) → validation gates → full trace`

- **Corpus**: synthetic HK recruitment firm "Meridian Search" — 132 docs
  (40 candidates, 12 client contracts, 60 placements, 20 misc), with a
  378-entity `ENTITY_MANIFEST.json` that is complete **by construction**
  (entities generated programmatically and injected verbatim; the LLM only
  wrote surrounding prose). The manifest is the leak-audit ground truth and
  is *never* read by the sanitizer.
- **Sanitizer** (`:8091`): deterministic regexes (HKID with checksum-format,
  HK phones, emails incl. `[at]/[dot]` obfuscation, HK$ amounts) → Presidio
  NER + name-pool gazetteer + Chinese-name pattern → local-LLM sweep
  (over-redact bias, fail-closed). Typed session-consistent placeholders,
  SQLite maps, exact rehydration.
- **Pipeline** (`:8092`): the sandwich above, plus an OpenAI-compatible shim
  so **Meridian-Hybrid** appears in Open WebUI's model picker next to the
  plain local model, and a single-file HTML **trace viewer** at
  `http://127.0.0.1:8092/traces` rendering every hop with the exact outbound
  payload highlighted.
- All services loopback-only, under the existing supervisor pattern, logs in
  `/workspace/logs/`, reboot-safe via `deploy/native-setup.sh`.

## Headline numbers

| Metric | Result |
|---|---|
| **Manifest entities in outbound frontier payloads** | **0 / 38 payloads (45-run benchmark + bring-up), independently re-grepped by auditor** |
| Leak-detector canary | 11/15 *unredacted* ceiling payloads DID contain entities — the grep provably detects |
| Sanitizer property test (30 held-out docs) | 0 leaks, 0 rehydration orphans |
| Over-redaction (false-positive) rate | 32.4% of redactions (fail-closed by design) |
| Benchmark C vs A (sandwich vs pure local) | **C wins 13/15** (1 loss, 1 tie) |
| Benchmark C vs B (sandwich vs unredacted frontier ceiling) | **C wins 10/15** (3 losses, 2 ties) |
| Median latency | A local 59.3s · B frontier 14.4s · C sandwich 113.8s (max 208s) |
| Frontier cost per sandwich query | **~$0.015** (avg 2,085 in / 597 out tokens, Sonnet 4.6) |

Win rates by tier (blind pairwise, Anthropic judge, randomized positions):

| Tier | C vs A | C vs B |
|---|---|---|
| Grounded lookup | 5–0 | 4–0 (1 tie) |
| Synthesis | 4–0 (1 tie) | 2–2 (1 tie) |
| Hard reasoning | 4–1 | 4–1 |

## Where the sandwich matched or beat the ceiling — and where it lost

The sandwich beat the *unredacted* frontier ceiling in 10/15 blind
comparisons because the **enrichment leg injects exact corpus figures with
citations** that the ceiling answer states more loosely. It lost 3: two
synthesis tasks where the ceiling's terser, better-prioritized memos won
(task 8: ceiling cited the same verified figures with sharper judgment), and
one reasoning task. Against pure-local it lost once (task 10, a
doc-grounded action list where local's directness won). Trace links: all 45
runs are in `/workspace/traces/pipeline.jsonl`, rendered at `/traces`.

Over-redaction's observed quality cost was low in this corpus: the frontier
reasons over typed placeholders ([PERSON_3], [AMOUNT_7]) and structure
survives; quality is recovered at enrichment. The 32.4% FP rate mostly
redacts role titles and doc-ids, which the placeholders preserve enough
context for.

## Honest limitations found

1. **The faithfulness gate needed two calibration cycles** (multi-citation
   claims must be judged against the union of cited chunks;
   paraphrase-tolerant 3-way verdicts). Its residual REVIEW flags are now
   genuine catches (miscited counts, meta-claims), but the local judge is
   still a 27B model with occasional strictness noise.
2. **Latency**: the sandwich is ~2x pure-local and ~8x raw frontier (median
   114s), dominated by the sanitizer's LLM sweep and the enrichment pass —
   both serialized through one GPU.
3. **Detection depends partly on non-deterministic layers**: lowercase/
   spaced HKIDs and novel obfuscations are caught by NER/LLM backstops, not
   the deterministic floor (auditor finding, Phase 2). Zero leaks held in
   all tests, but the guarantee is probabilistic beyond layer 1.

**Top 3 fixes, effort-estimated**
1. Batch the sanitizer LLM sweep + enrichment into single vLLM calls with
   prefix caching — should cut sandwich median under 60s (~half a day).
2. Replace the LLM faithfulness judge with an NLI cross-encoder on CPU
   (deterministic, faster, frees GPU) (~1 day).
3. Widen layer-1: checksum-validate HKIDs, IBAN/passport patterns,
   fuzzy-name matching against the gazetteer to cut FP rate and reliance on
   the LLM sweep (~1–2 days).

## 3-minute demo script

1. Open Open WebUI → model picker → select **Meridian-Hybrid** (next to the
   plain local model). Ask: *"What fee and rebate terms did we agree with
   Atlas Global Bank?"* — answer arrives with real figures and [CLI-01]
   citations.
2. In a terminal: `ssh -p <port> root@<host> -L 8092:127.0.0.1:8092`, open
   `http://localhost:8092/traces` — the newest trace shows every hop; the
   **amber block is the exact payload that left the box**: point at
   [ORG_1]/[AMOUNT_2] placeholders — no names, no numbers.
3. Ask the plain local model the same question for the speed/quality
   contrast, then show the headline: **0 manifest entities in 38 outbound
   payloads**, with the unredacted-ceiling canary proving the detector
   works.

## Reproduce

```
services/privacy-pipeline/
  gen_corpus.py     corpus + manifest    test_sanitizer.py  property test
  sanitizer.py      :8091                benchmark.py       45-run benchmark
  pipeline.py       :8092 + viewer       ingest.py          Qdrant ingest
```
Deploy: `deploy/native-setup.sh` (services `sanitizer`, `pipeline`,
`qdrant` under supervisors). Secrets in `deploy/.env` (never committed).
