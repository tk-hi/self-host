#!/usr/bin/env bash
# Step 3 — push the stack to the instance and start it.
#   ./03-deploy.sh <INSTANCE_ID>
# Expects deploy/.env to exist locally (secrets live there, never in git).
#
# vast.ai container instances are unprivileged containers: no nested docker,
# so the compose file cannot run there. This deploys the same topology
# natively via deploy/native-setup.sh — vLLM pinned to 127.0.0.1:8000
# (vast maps only 22 and ${WEBUI_PORT}, so it stays internal either way),
# Open WebUI on 0.0.0.0:${WEBUI_PORT}. deploy/docker-compose.yml remains the
# reference topology and works as-is on VM offers or any real docker host.
set -euo pipefail

: "${VAST_API_KEY:?export VAST_API_KEY first}"
INSTANCE_ID="${1:?usage: 03-deploy.sh <INSTANCE_ID>}"
VASTAI="${VASTAI:-vastai}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"

[ -f "${HERE}/deploy/.env" ] || { echo "missing deploy/.env — copy deploy/.env.example and fill it in"; exit 1; }

read -r SSH_HOST SSH_PORT < <("$VASTAI" show instance "$INSTANCE_ID" --raw | jq -r '"\(.ssh_host) \(.ssh_port)"')
SSH_OPTS=(-o StrictHostKeyChecking=accept-new -p "$SSH_PORT")
SCP_OPTS=(-o StrictHostKeyChecking=accept-new -P "$SSH_PORT")
TARGET="root@${SSH_HOST}"

echo "==> ${TARGET}:${SSH_PORT}"

ssh "${SSH_OPTS[@]}" "$TARGET" "mkdir -p /workspace/stack && nvidia-smi --query-gpu=name,memory.total --format=csv,noheader"

scp "${SCP_OPTS[@]}" "${HERE}/deploy/.env" "${HERE}/deploy/native-setup.sh" "${TARGET}:/workspace/stack/"
scp -r "${SCP_OPTS[@]}" "${HERE}/services/pdf-renderer" "${TARGET}:/workspace/stack/"

# Installs uv + Python 3.12 venvs for vLLM and Open WebUI (idempotent), writes
# the two launchers, and starts both under restart-loop supervisors.
ssh "${SSH_OPTS[@]}" "$TARGET" "bash /workspace/stack/native-setup.sh"

cat <<NOTE

Started. The first boot downloads the model weights (tens of GB), so vLLM
will not answer for a while — that is expected, not a failure. Follow it with:

  ./scripts/poll-ready.sh ${INSTANCE_ID}

Logs on the instance: /workspace/logs/{vllm,open-webui}.log
NOTE
