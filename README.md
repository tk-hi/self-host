# Self-hosted ChatGPT-style stack on vast.ai

vLLM (OpenAI-compatible, tool calling, 64k context) behind Open WebUI, with
Tavily web search and the Pyodide code interpreter, deployed to a rented
vast.ai GPU instance (natively — see Layout; the compose file is the
reference topology for real docker hosts).

## Status: deployed and verified

Deployed 2026-08-22 from a machine with full network access, after restoring
the correct model (see [Which model](#which-model)). An A100 rental hung in
`loading` and was destroyed; the stack was brought up on a rented RTX 4090
24GB (verified datacenter host, Hungary, ~$0.68/hr) with the
`rtx4090-24gb.env` profile and passed all four checks in `scripts/04-verify.sh`.
The instance is billed hourly until destroyed — `vastai destroy instance <ID>`
ends it (stopping is not enough; storage keeps billing).

## Layout

```
deploy/docker-compose.yml       reference topology (works on any real docker host)
deploy/native-setup.sh          what actually runs on a vast.ai container instance
deploy/.env.example             template for secrets + model settings
deploy/profiles/*.env           per-GPU model settings, drop into .env
scripts/01-search-offers.sh     list rentable offers (rents nothing)
scripts/02-rent.sh              rent a chosen offer
scripts/03-deploy.sh            push the stack over SSH and start it (native)
scripts/poll-ready.sh           wait out the multi-GB weight download
scripts/04-verify.sh            the four verification checks
scripts/05-destroy.sh           end all billing
```

vast.ai container instances are unprivileged containers — nested docker is
not possible there (discovered on first deploy; `docker` is absent and the
instance has no NET_ADMIN). `03-deploy.sh` therefore installs vLLM and Open
WebUI natively via `uv` (Python 3.12 venvs under `/workspace/venvs`) and runs
both under restart-loop supervisors, logging to `/workspace/logs/`. The
compose file is kept as the reference topology for VM offers or self-managed
docker hosts.

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

**`Qwen/Qwen3.8-27B`** — released 2026-08-05 (Apache 2.0), verified directly
against the Hugging Face API. An earlier revision of this repo swapped it for
Qwen3-30B-A3B in the mistaken belief the 3.8 series did not exist; that swap
has been reverted everywhere.

Qwen3.8-27B is a dense 27B vision-language model built on the Qwen3.5 hybrid
architecture: 48 of its 64 layers use Gated DeltaNet linear attention and only
16 use full attention (4 KV heads x 256 head dim), so the KV cache is ~64KB
per token in bf16 — tiny for its class. That is what lets a 4-bit quant run
64k context on a 24GB card. Serving needs vLLM 0.17.0+ with
`--reasoning-parser qwen3` and `--tool-call-parser qwen3_coder`; the compose
file pins `vllm/vllm-openai:qwen38`, the image the official vLLM recipe
recommends for this family.

| Profile | GPU | Model | Notes |
|---|---|---|---|
| `a100-80gb.env` | A100 80GB | `Qwen/Qwen3.8-27B-FP8` | Official block-scaled FP8 (~29GiB), 64k context, vision kept. Best option. |
| `rtx4090-24gb.env` | RTX 4090 24GB | `cyankiwi/Qwen3.8-27B-AWQ-INT4` | ~21GB AWQ 4-bit + fp8 KV + `--language-model-only`. Fits 64k with a thin margin. |
| `rtx4090-24gb-safe.env` | RTX 4090 24GB | `Qwen/Qwen3.5-27B-GPTQ-Int4` | Official quant of the previous-gen 27B (~17GB). Comfortable fallback. |

On the 4090 quant choice: no cpatonn AWQ exists for this model, and Unsloth's
vLLM-compatible quants are NVFP4 (Blackwell-only) and GGUF (weak vLLM
support). cyankiwi's AWQ-INT4 (385k downloads) is the de-facto community AWQ.
Note it is packaged in **compressed-tensors** format, not classic AWQ — do
NOT pass `--quantization awq_marlin` (vLLM rejects the mismatch and
crash-loops); let vLLM auto-detect from the model config.

## Exposure

Only Open WebUI is published. In the native deployment vLLM binds
`127.0.0.1:8000`, and vast.ai only maps ports 22 and `${WEBUI_PORT}` anyway —
two independent reasons it is unreachable from outside. (In the compose
variant the same is achieved with `expose:` instead of `ports:`.)
`scripts/04-verify.sh` asserts this by checking that port 8000 does *not*
answer on the public IP.

vLLM is still protected by `VLLM_API_KEY` even though it is internal, so
nothing else sharing the instance can use the GPU for free.

## Cost

Live numbers from the 2026-08-22 deployment search (verified DC hosts,
100GB disk; `01-search-offers.sh` prints current ones):

| | GPU | Storage (100GB) | Total |
|---|---|---|---|
| RTX 4090, verified DC (rented) | $0.40–0.64/hr | ~$0.03–0.04/hr | **~$0.43–0.68/hr** |
| A100 80GB SXM | $1.04/hr | ~$0.03/hr | **~$1.07/hr** |

Plus bandwidth; the ~29GB initial download is billed on some hosts.

## PDF rendering (services/pdf-renderer)

A loopback-only FastAPI microservice (`127.0.0.1:8090`) that renders
structured JSON into typeset A4 PDFs via Jinja2 + WeasyPrint, with Source
Serif 4 bundled (OFL) so no system fonts are needed. Two doc types:
`workflow_atlas` (cover, per-section divider pages, one workflow per page)
and `memo` (cover + flowing sections). Both share `templates/_base.css.j2` —
cover, footer-with-confidentiality-line + page numbers on every page
(CSS `@page` margin boxes), and accent-color rules. Adding a doc type means
adding a template and one entry in `DOC_TEMPLATES`.

```bash
curl -s -X POST http://127.0.0.1:8090/render \
  -H 'Content-Type: application/json' \
  -d @services/pdf-renderer/sample-payload.json -o out.pdf
```

Rendered PDFs are also saved to `/workspace/outputs/pdfs/` and served back
at `GET /files/{name}`. WeasyPrint's pango/cairo/gdk-pixbuf system libs are
apt-installed by `deploy/native-setup.sh` (root inside the container, so apt
works; if a host ever blocks it, swap WeasyPrint for ReportLab).

`services/pdf-renderer/openwebui_tool.py` is an Open WebUI tool
(`generate_pdf`) that POSTs to the service and uploads the result into Open
WebUI's file storage so the chat reply carries a real download link. It
needs an Open WebUI API key in its Valves (the tool uploads files on your
behalf), and must be enabled for the model in the UI or per-chat via the
tools (+) menu.

## Ending billing

```bash
vastai destroy instance <INSTANCE_ID>
```

`vastai stop instance` is **not** enough — a stopped instance keeps billing for
its disk. Only `destroy` releases storage. Verify with `vastai show instances`;
an empty list means you are no longer being charged.

## History: why the first attempt could not deploy

This repo was originally prepared in a network-restricted sandbox whose
egress policy denied (HTTP 403 at the proxy):

- `console.vast.ai` and `api.vast.ai` — the vast.ai API, so no search, no rent
- `huggingface.co` — could not verify model ids or quant availability
- `api.tavily.com` — could not smoke-test the search key
- `docker.io` — could not pull the vLLM image
- outbound SSH to non-443 ports — vast.ai instances listen on high ports, so
  even a rented instance would not have been reachable to deploy to

Only `github.com`, `ghcr.io` and the package registries were permitted. That
restriction also produced the incorrect model swap fixed in
[Which model](#which-model): with huggingface.co unreachable, `Qwen/Qwen3.8-27B`
(released 2026-08-05, after the preparing model's knowledge cutoff) was
presumed not to exist.

## Secrets

`deploy/.env` is gitignored and was never created in this repo. The vast.ai and
Tavily keys shared in the task prompt are not stored here — put them in
`deploy/.env` yourself at deploy time. Since they were pasted into a chat,
rotating them afterwards is the safe move.
