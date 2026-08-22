#!/usr/bin/env bash
# Patiently wait for vLLM to finish downloading weights and start serving.
# Polls for up to 90 minutes; prints download progress from the vLLM log.
set -euo pipefail

: "${VAST_API_KEY:?export VAST_API_KEY first}"
INSTANCE_ID="${1:?usage: poll-ready.sh <INSTANCE_ID>}"
VASTAI="${VASTAI:-vastai}"
read -r SSH_HOST SSH_PORT < <("$VASTAI" show instance "$INSTANCE_ID" --raw | jq -r '"\(.ssh_host) \(.ssh_port)"')
SSH_OPTS=(-o StrictHostKeyChecking=accept-new -p "$SSH_PORT")

for i in $(seq 1 180); do
  state=$(ssh "${SSH_OPTS[@]}" "root@${SSH_HOST}" \
    "docker inspect -f '{{.State.Health.Status}}' vllm 2>/dev/null || echo absent")
  printf '[%3d] vllm health=%s  ' "$i" "$state"
  ssh "${SSH_OPTS[@]}" "root@${SSH_HOST}" \
    "docker logs --tail 1 vllm 2>&1 | tr -d '\r' | cut -c1-100" || true
  [ "$state" = "healthy" ] && { echo "vLLM is serving."; exit 0; }
  sleep 30
done

echo "Still not healthy after 90 minutes — check: docker logs vllm"
exit 1
