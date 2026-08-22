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
mkdir -p "$HF_HOME" /workspace/webui-data /workspace/logs

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

# --- vLLM launcher (internal only: 127.0.0.1) ------------------------------
cat > /workspace/stack/run-vllm.sh <<LAUNCH
#!/usr/bin/env bash
set -a; . /workspace/stack/.env; set +a
export HF_HOME="\${HF_CACHE_DIR:-/workspace/hf-cache}"
export HF_HUB_ENABLE_HF_TRANSFER=\${HF_TRANSFER:-1}
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
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

chmod +x /workspace/stack/run-vllm.sh /workspace/stack/run-webui.sh

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

echo "started. logs: /workspace/logs/{vllm,open-webui}.log"
