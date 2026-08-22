#!/usr/bin/env bash
# Step 4 — prove the stack actually works before calling it done.
#   ./04-verify.sh <INSTANCE_ID>
# Runs the vLLM checks over SSH (vLLM is loopback-only, by design) and the
# Open WebUI checks against the public port.
set -euo pipefail

: "${VAST_API_KEY:?export VAST_API_KEY first}"
INSTANCE_ID="${1:?usage: 04-verify.sh <INSTANCE_ID>}"
VASTAI="${VASTAI:-vastai}"
WEBUI_PORT="${WEBUI_PORT:-8080}"

info=$("$VASTAI" show instance "$INSTANCE_ID" --raw)
SSH_HOST=$(jq -r '.ssh_host' <<<"$info")
SSH_PORT=$(jq -r '.ssh_port' <<<"$info")
PUB_IP=$(jq -r '.public_ipaddr' <<<"$info")
EXT_PORT=$(jq -r --arg p "${WEBUI_PORT}/tcp" '.ports[$p][0].HostPort // empty' <<<"$info")
SSH_OPTS=(-o StrictHostKeyChecking=accept-new -p "$SSH_PORT")
WEBUI_URL="http://${PUB_IP}:${EXT_PORT}"

echo "=============================================="
echo "1/4  vLLM lists the model (loopback, over SSH)"
echo "=============================================="
ssh "${SSH_OPTS[@]}" "root@${SSH_HOST}" bash -s <<'REMOTE'
set -euo pipefail
set -a; . /workspace/stack/.env; set +a
curl -fsS -H "Authorization: Bearer ${VLLM_API_KEY}" \
  http://127.0.0.1:8000/v1/models | jq '.data[] | {id, max_model_len}'
REMOTE

echo
echo "=============================================="
echo "2/4  vLLM test completion"
echo "=============================================="
ssh "${SSH_OPTS[@]}" "root@${SSH_HOST}" bash -s <<'REMOTE'
set -euo pipefail
set -a; . /workspace/stack/.env; set +a
curl -fsS -H "Authorization: Bearer ${VLLM_API_KEY}" -H 'Content-Type: application/json' \
  -d "{\"model\":\"${SERVED_MODEL_NAME}\",\"messages\":[{\"role\":\"user\",\"content\":\"In one sentence: what is a KV cache?\"}],\"max_tokens\":600}" \
  http://127.0.0.1:8000/v1/chat/completions | jq -r '.choices[0].message.content'
REMOTE

echo
echo "=============================================="
echo "3/4  vLLM tool calling"
echo "=============================================="
ssh "${SSH_OPTS[@]}" "root@${SSH_HOST}" bash -s <<'REMOTE'
set -euo pipefail
set -a; . /workspace/stack/.env; set +a
curl -fsS -H "Authorization: Bearer ${VLLM_API_KEY}" -H 'Content-Type: application/json' \
  -d "{\"model\":\"${SERVED_MODEL_NAME}\",
       \"messages\":[{\"role\":\"user\",\"content\":\"What is the weather in Oslo?\"}],
       \"tools\":[{\"type\":\"function\",\"function\":{\"name\":\"get_weather\",
         \"description\":\"Current weather for a city\",
         \"parameters\":{\"type\":\"object\",\"properties\":{\"city\":{\"type\":\"string\"}},\"required\":[\"city\"]}}}],
       \"tool_choice\":\"auto\"}" \
  http://127.0.0.1:8000/v1/chat/completions | jq '.choices[0].message.tool_calls'
REMOTE

echo
echo "=============================================="
echo "3.5/4  pdf-renderer health (loopback, over SSH)"
echo "=============================================="
ssh "${SSH_OPTS[@]}" "root@${SSH_HOST}" \
  "curl -fsS http://127.0.0.1:8090/health"
echo

echo
echo "=============================================="
echo "4/4  Open WebUI reachable on the public port"
echo "=============================================="
curl -fsS -o /dev/null -w "  GET %{url_effective} -> %{http_code}\n" "${WEBUI_URL}/health" \
  || curl -fsS -o /dev/null -w "  GET %{url_effective} -> %{http_code}\n" "${WEBUI_URL}/"

echo
echo "  vLLM must NOT be publicly reachable — expect this to fail:"
if curl -fsS --max-time 8 -o /dev/null "http://${PUB_IP}:8000/v1/models" 2>/dev/null; then
  echo "  !! WARNING: vLLM answered on the public IP. It must bind 127.0.0.1 only."
else
  echo "  OK: port 8000 is not publicly served."
fi
if curl -fsS --max-time 8 -o /dev/null "http://${PUB_IP}:8090/health" 2>/dev/null; then
  echo "  !! WARNING: pdf-renderer answered on the public IP. It must bind 127.0.0.1 only."
else
  echo "  OK: port 8090 is not publicly served."
fi

cat <<NOTE

Open WebUI: ${WEBUI_URL}

Remaining checks are done in the browser, once you have created your admin
account (first signup becomes admin):
  - the model "\$SERVED_MODEL_NAME" appears in the model picker
  - Settings > Web Search shows engine=tavily, enabled
  - toggle Web Search in a chat and ask a question that needs fresh facts;
    the reply should carry inline citations
  - ask for a calculation "using the code interpreter"; a Pyodide block runs
NOTE
