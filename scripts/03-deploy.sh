#!/usr/bin/env bash
# Step 3 — push the compose stack to the instance and start it.
#   ./03-deploy.sh <INSTANCE_ID>
# Expects deploy/.env to exist locally (secrets live there, never in git).
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

# The vast base images ship docker but not always the compose plugin.
ssh "${SSH_OPTS[@]}" "$TARGET" bash -s <<'REMOTE'
set -euo pipefail
mkdir -p /workspace/stack /workspace/hf-cache
if ! docker compose version >/dev/null 2>&1; then
  echo "installing docker compose plugin"
  apt-get update -qq
  apt-get install -y -qq docker-compose-plugin || {
    mkdir -p /usr/local/lib/docker/cli-plugins
    curl -fsSL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64" \
      -o /usr/local/lib/docker/cli-plugins/docker-compose
    chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
  }
fi
docker compose version
nvidia-smi --query-gpu=name,memory.total --format=csv
REMOTE

scp "${SCP_OPTS[@]}" "${HERE}/deploy/docker-compose.yml" "${HERE}/deploy/.env" "${TARGET}:/workspace/stack/"

ssh "${SSH_OPTS[@]}" "$TARGET" bash -s <<'REMOTE'
set -euo pipefail
cd /workspace/stack
docker compose pull
# Start vLLM first and let it download weights; open-webui waits on its health.
docker compose up -d
echo "--- containers ---"
docker compose ps
REMOTE

cat <<NOTE

Started. The first boot downloads ~30GB, so vLLM will sit in 'health: starting'
for a while — that is expected, not a failure. Follow it with:

  ./scripts/poll-ready.sh ${INSTANCE_ID}
NOTE
