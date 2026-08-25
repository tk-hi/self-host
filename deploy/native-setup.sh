#!/usr/bin/env bash
# Native (no-docker) deployment for vast.ai container instances, which do not
# allow nested docker. Same topology as docker-compose.yml: vLLM is bound to
# 127.0.0.1 (vast maps only 22 and ${WEBUI_PORT}, so it is unreachable from
# outside either way); Open WebUI listens on 0.0.0.0:${WEBUI_PORT}.
# Expects /workspace/stack/.env (scp'd by 03-deploy.sh).
set -euo pipefail

cd /workspace/stack
set -a; . ./.env; set +a

export HF_HOME="${HF_CACHE_DIR:-/workspace/hf-cache}"
# Some base images (e.g. cuda-12.8.1-auto) do not pre-create /workspace.
mkdir -p /workspace/stack "$HF_HOME" /workspace/webui-data /workspace/logs \
  /workspace/corpus/docs /workspace/traces /workspace/outputs/pdfs

# --- Blackwell (RTX 5090 / SM 12.x) needs a CUDA >= 12.9 toolkit -----------
# vLLM hard-requires FlashInfer for the Qwen3.8 GDN model, and FlashInfer
# JIT-compiles its sm_120 kernels with the system nvcc. A CUDA 12.8 image
# fails with "SM 12.x requires CUDA >= 12.9". Install cuda-toolkit-13-0 and
# expose it to the vLLM launcher below via CUDA_HOME/TORCH_CUDA_ARCH_LIST.
CUDA_EXTRA_ENV=""
if command -v nvidia-smi >/dev/null 2>&1; then
  CAP=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -1 | tr -d '.')
  if [ "${CAP:-0}" -ge 120 ] && [ ! -x /usr/local/cuda-13.0/bin/nvcc ]; then
    echo "==> Blackwell GPU (compute_cap ${CAP}) detected; installing cuda-toolkit-13-0"
    apt-get update -qq
    apt-get install -y -qq cuda-toolkit-13-0
  fi
  if [ -x /usr/local/cuda-13.0/bin/nvcc ]; then
    CUDA_EXTRA_ENV=$'export CUDA_HOME=/usr/local/cuda-13.0\nexport PATH="$CUDA_HOME/bin:$PATH"\nexport TORCH_CUDA_ARCH_LIST="12.0"'
  fi
fi

# uv manages the Python 3.12 both services need (base image ships 3.10).
if ! command -v uv >/dev/null 2>&1; then
  curl -fsSL https://astral.sh/uv/install.sh | sh >/dev/null
fi
export PATH="$HOME/.local/bin:$PATH"

if [ ! -x /workspace/venvs/vllm/bin/vllm ]; then
  uv venv --python 3.12 /workspace/venvs/vllm
  uv pip install --python /workspace/venvs/vllm/bin/python vllm hf_transfer
fi

if [ ! -x /workspace/venvs/webui/bin/open-webui ]; then
  uv venv --python 3.12 /workspace/venvs/webui
  uv pip install --python /workspace/venvs/webui/bin/python open-webui
fi

# --- pdf-renderer: WeasyPrint needs pango/cairo/gdk-pixbuf system libs ------
# We are root inside the (unprivileged) container, so apt works. If a host
# ever blocks apt, swap WeasyPrint for ReportLab in the service.
if ! dpkg -s libpango-1.0-0 >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y -qq libpango-1.0-0 libpangocairo-1.0-0 libpangoft2-1.0-0 \
    libcairo2 libgdk-pixbuf-2.0-0 libffi-dev shared-mime-info
fi

if [ ! -x /workspace/venvs/pdf/bin/uvicorn ]; then
  uv venv --python 3.12 /workspace/venvs/pdf
  uv pip install --python /workspace/venvs/pdf/bin/python \
    -r /workspace/stack/pdf-renderer/requirements.txt
fi
mkdir -p /workspace/outputs/pdfs

# --- privacy-pipeline venv (presidio + spaCy + llama-index + llm-guard) ------
# The "sandwich" services (sanitizer :8091, pipeline :8092) run on CPU: their
# torch (2.3.1) predates Blackwell and the corpus is tiny, so embeddings/NER
# run on CPU (run-sanitizer/run-pipeline set CUDA_VISIBLE_DEVICES=""). The
# pinned requirements.txt reproduces the exact working set incl. en_core_web_lg.
if [ -d /workspace/stack/privacy-pipeline ] && [ ! -x /workspace/venvs/pipeline/bin/uvicorn ]; then
  uv venv --python 3.12 /workspace/venvs/pipeline
  uv pip install --python /workspace/venvs/pipeline/bin/python \
    -r /workspace/stack/privacy-pipeline/requirements.txt
fi

# --- Qdrant (vector DB for the privacy sandwich; loopback only) --------------
if [ -d /workspace/stack/privacy-pipeline ] && [ ! -x /workspace/qdrant/qdrant ]; then
  QVER="${QDRANT_VERSION:-1.19.0}"
  mkdir -p /workspace/qdrant/storage
  curl -fsSL "https://github.com/qdrant/qdrant/releases/download/v${QVER}/qdrant-x86_64-unknown-linux-gnu.tar.gz" \
    | tar xz -C /workspace/qdrant
fi
if [ -x /workspace/qdrant/qdrant ]; then
cat > /workspace/stack/run-qdrant.sh <<'LAUNCH'
#!/usr/bin/env bash
cd /workspace/qdrant
export QDRANT__SERVICE__HOST=127.0.0.1 QDRANT__SERVICE__HTTP_PORT=6333
export QDRANT__STORAGE__STORAGE_PATH=/workspace/qdrant/storage
exec ./qdrant
LAUNCH
chmod +x /workspace/stack/run-qdrant.sh
fi

# --- vLLM launcher (internal only: 127.0.0.1) ------------------------------
cat > /workspace/stack/run-vllm.sh <<LAUNCH
#!/usr/bin/env bash
set -a; . /workspace/stack/.env; set +a
export HF_HOME="\${HF_CACHE_DIR:-/workspace/hf-cache}"
export HF_HUB_ENABLE_HF_TRANSFER=\${HF_TRANSFER:-1}
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
${CUDA_EXTRA_ENV}
exec /workspace/venvs/vllm/bin/vllm serve "\${MODEL_ID}" \
  --served-model-name "\${SERVED_MODEL_NAME}" \
  --host 127.0.0.1 --port 8000 \
  --api-key "\${VLLM_API_KEY}" \
  --max-model-len "\${MAX_MODEL_LEN:-65536}" \
  --gpu-memory-utilization "\${GPU_MEM_UTIL:-0.92}" \
  --kv-cache-dtype "\${KV_CACHE_DTYPE:-auto}" \
  --enable-auto-tool-choice \
  --tool-call-parser "\${TOOL_PARSER:-qwen3_coder}" \
  --reasoning-parser "\${REASONING_PARSER:-qwen3}" \
  --enable-prefix-caching \
  \${EXTRA_VLLM_ARGS:-}
LAUNCH

# --- Open WebUI launcher (public port) --------------------------------------
cat > /workspace/stack/run-webui.sh <<LAUNCH
#!/usr/bin/env bash
set -a; . /workspace/stack/.env; set +a
export DATA_DIR=/workspace/webui-data
export ENABLE_OPENAI_API=true
export OPENAI_API_BASE_URL=http://127.0.0.1:8000/v1
export OPENAI_API_KEY="\${VLLM_API_KEY}"
export ENABLE_OLLAMA_API=false
export WEBUI_AUTH=true
export ENABLE_WEB_SEARCH=true ENABLE_RAG_WEB_SEARCH=true
export WEB_SEARCH_ENGINE=tavily RAG_WEB_SEARCH_ENGINE=tavily
export WEB_SEARCH_RESULT_COUNT=5 RAG_WEB_SEARCH_RESULT_COUNT=5
export WEB_SEARCH_CONCURRENT_REQUESTS=4 ENABLE_SEARCH_QUERY_GENERATION=true
export ENABLE_CODE_EXECUTION=true CODE_EXECUTION_ENGINE=pyodide
export ENABLE_CODE_INTERPRETER=true CODE_INTERPRETER_ENGINE=pyodide
exec /workspace/venvs/webui/bin/open-webui serve --host 0.0.0.0 --port "\${WEBUI_PORT:-8080}"
LAUNCH

# --- privacy pipeline launchers (loopback only) ------------------------------
if [ -d /workspace/stack/privacy-pipeline ]; then
cat > /workspace/stack/run-sanitizer.sh <<'LAUNCH'
#!/usr/bin/env bash
set -a; . /workspace/stack/.env; set +a
export CUDA_VISIBLE_DEVICES=""
cd /workspace/stack/privacy-pipeline
export HF_HOME=/workspace/hf-cache
exec /workspace/venvs/pipeline/bin/uvicorn sanitizer:app --host 127.0.0.1 --port 8091
LAUNCH
cat > /workspace/stack/run-pipeline.sh <<'LAUNCH'
#!/usr/bin/env bash
set -a; . /workspace/stack/.env; set +a
export CUDA_VISIBLE_DEVICES=""
cd /workspace/stack/privacy-pipeline
export HF_HOME=/workspace/hf-cache
exec /workspace/venvs/pipeline/bin/uvicorn pipeline:app --host 127.0.0.1 --port 8092
LAUNCH
chmod +x /workspace/stack/run-sanitizer.sh /workspace/stack/run-pipeline.sh
fi

# --- pdf-renderer launcher (internal only: 127.0.0.1) ------------------------
cat > /workspace/stack/run-pdf-renderer.sh <<'LAUNCH'
#!/usr/bin/env bash
cd /workspace/stack/pdf-renderer
export PDF_OUTPUT_DIR=/workspace/outputs/pdfs
exec /workspace/venvs/pdf/bin/uvicorn app:app --host 127.0.0.1 --port 8090
LAUNCH

chmod +x /workspace/stack/run-vllm.sh /workspace/stack/run-webui.sh /workspace/stack/run-pdf-renderer.sh

# --- supervise with restart loops (no systemd in a container instance) ------
sup() {  # sup <name> <script>
  pkill -f "supervise-$1" 2>/dev/null || true
  nohup bash -c "
    # supervise-$1
    while true; do
      $2 >> /workspace/logs/$1.log 2>&1
      echo \"[supervisor] $1 exited \$? — restarting in 10s\" >> /workspace/logs/$1.log
      sleep 10
    done" > /dev/null 2>&1 &
}

sup vllm /workspace/stack/run-vllm.sh
sup open-webui /workspace/stack/run-webui.sh
sup pdf-renderer /workspace/stack/run-pdf-renderer.sh
if [ -x /workspace/stack/run-sanitizer.sh ]; then
  sup sanitizer /workspace/stack/run-sanitizer.sh
  sup pipeline /workspace/stack/run-pipeline.sh
fi
if [ -x /workspace/stack/run-qdrant.sh ]; then
  sup qdrant /workspace/stack/run-qdrant.sh
fi

echo "started. logs: /workspace/logs/{vllm,open-webui}.log"
