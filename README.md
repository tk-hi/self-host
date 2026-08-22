# Self-hosted ChatGPT-style stack on vast.ai

vLLM (OpenAI-compatible, tool calling, 64k context) behind Open WebUI, with
Tavily web search and the Pyodide code interpreter, deployed to a rented
vast.ai GPU instance via Docker Compose.

## Status: not deployed

Nothing has been rented and nothing is running. The sandbox this was prepared
in cannot reach the vast.ai API — see [Why it is not deployed](#why-it-is-not-deployed).
Everything below is ready to run from a machine with normal outbound network
access; the compose file and both env-loading paths are validated
(`docker compose config` renders correctly for every profile).

## Layout

```
deploy/docker-compose.yml       the stack
deploy/.env.example             template for secrets + model settings
deploy/profiles/*.env           per-GPU model settings, drop into .env
scripts/01-search-offers.sh     list rentable offers (rents nothing)
scripts/02-rent.sh              rent a chosen offer
scripts/03-deploy.sh            push the stack over SSH and start it
scripts/poll-ready.sh           wait out the ~30GB weight download
scripts/04-verify.sh            the four verification checks
scripts/05-destroy.sh           end all billing
```

## Runbook

```bash
pip install vastai
export VAST_API_KEY=...            # your vast.ai key

./scripts/01-search-offers.sh      # pick an offer id from the tables
./scripts/02-rent.sh <OFFER_ID>    # prints INSTANCE_ID

cp deploy/.env.example deploy/.env
cat deploy/profiles/a100-80gb.env >> deploy/.env   # or the 4090 profile
# fill in VLLM_API_KEY, WEBUI_SECRET_KEY, TAVILY_API_KEY
#   openssl rand -hex 32

./scripts/03-deploy.sh <INSTANCE_ID>
./scripts/poll-ready.sh <INSTANCE_ID>    # 20-60 min on first boot
./scripts/04-verify.sh <INSTANCE_ID>
```

Then open the printed URL and create your account — **the first account to
sign up becomes the admin**. Afterwards set `ENABLE_SIGNUP=false` in
`deploy/.env` and `docker compose up -d` again to seal it.

## Which model

`Qwen/Qwen3.8-27B` does not exist — there is no Qwen 3.8 series, and no 27B
Qwen3. (27B is a Gemma 3 size; Qwen3's nearby sizes are 14B, 30B-A3B and 32B.)
The profiles target the closest real model, **Qwen3-30B-A3B-Instruct-2507**, a
30B mixture-of-experts with 3B active parameters — roughly 32B-class quality at
far higher throughput, which is what you want on a single rented GPU.

| Profile | GPU | Model | Notes |
|---|---|---|---|
| `a100-80gb.env` | A100 80GB | `Qwen3-30B-A3B-Instruct-2507-FP8` | Official FP8 weights (~32GB), 64k context, no quant flags. Best option. |
| `rtx4090-24gb.env` | RTX 4090 24GB | AWQ 4-bit community quant | ~17GB weights + fp8 KV cache. Fits 64k, but with a thin margin. |
| `rtx4090-24gb-safe.env` | RTX 4090 24GB | `Qwen3-14B-AWQ` | Comfortable fallback if the 30B AWQ will not stay up. |

Sizing note for the 4090: a **dense 32B at 64k does not fit**. Qwen3-32B's KV
cache alone is ~256KB/token, so 64k context needs ~16GB on top of ~19GB of
4-bit weights. The MoE model's cache is ~96KB/token, which is why it is the
one that fits.

The AWQ repo id in `rtx4090-24gb.env` is a community quant and I could not
verify it is still published (huggingface.co is blocked here too). Confirm it
resolves before relying on the 4090 path, or swap in any other AWQ/GPTQ quant
of the same base model.

## Exposure

Only Open WebUI is published. vLLM is declared with `expose:` rather than
`ports:`, so it is reachable at `http://vllm:8000` on the compose network and
has no host binding for vast.ai to map. `scripts/04-verify.sh` asserts this by
checking that port 8000 does *not* answer on the public IP.

vLLM is still protected by `VLLM_API_KEY` even though it is internal, so
nothing else sharing the instance can use the GPU for free.

## Cost

Rough ranges, from prior knowledge rather than a live query — `01-search-offers.sh`
prints the real numbers:

| | GPU | Storage (100GB) | Typical total |
|---|---|---|---|
| RTX 4090, verified DC | ~$0.30–0.50/hr | ~$0.01–0.03/hr | **~$0.35–0.55/hr** |
| A100 80GB SXM | ~$0.70–1.10/hr | ~$0.01–0.03/hr | **~$0.75–1.15/hr** |

Plus bandwidth; the ~30GB initial download is billed on some hosts.

## Ending billing

```bash
vastai destroy instance <INSTANCE_ID>
```

`vastai stop instance` is **not** enough — a stopped instance keeps billing for
its disk. Only `destroy` releases storage. Verify with `vastai show instances`;
an empty list means you are no longer being charged.

## Why it is not deployed

The sandbox's egress policy denies (HTTP 403 at the proxy):

- `console.vast.ai` and `api.vast.ai` — the vast.ai API, so no search, no rent
- `huggingface.co` — could not verify model ids or quant availability
- `api.tavily.com` — could not smoke-test the search key
- `docker.io` — could not pull the vLLM image
- outbound SSH to non-443 ports — vast.ai instances listen on high ports, so
  even a rented instance would not have been reachable to deploy to

Only `github.com`, `ghcr.io` and the package registries are permitted. These
are organization policy denials, so they need an allowlist change rather than
a retry.

## Secrets

`deploy/.env` is gitignored and was never created in this repo. The vast.ai and
Tavily keys shared in the task prompt are not stored here — put them in
`deploy/.env` yourself at deploy time. Since they were pasted into a chat,
rotating them afterwards is the safe move.
