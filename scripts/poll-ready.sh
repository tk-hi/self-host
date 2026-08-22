#!/usr/bin/env bash
# Patiently wait for vLLM to finish downloading weights and start serving.
# Polls for up to 90 minutes; prints the tail of the vLLM log while waiting
# and bails early on a genuinely fatal error (OOM, CUDA failure).
set -euo pipefail

: "${VAST_API_KEY:?export VAST_API_KEY first}"
INSTANCE_ID="${1:?usage: poll-ready.sh <INSTANCE_ID>}"
VASTAI="${VASTAI:-vastai}"
read -r SSH_HOST SSH_PORT < <("$VASTAI" show instance "$INSTANCE_ID" --raw | jq -r '"\(.ssh_host) \(.ssh_port)"')
SSH_OPTS=(-o StrictHostKeyChecking=accept-new -p "$SSH_PORT")

for i in $(seq 1 180); do
  out=$(ssh "${SSH_OPTS[@]}" "root@${SSH_HOST}" bash -s <<'REMOTE'
set -a; . /workspace/stack/.env; set +a
if curl -fsS -m 5 -H "Authorization: Bearer ${VLLM_API_KEY}" http://127.0.0.1:8000/v1/models >/dev/null 2>&1; then
  echo "READY"
elif grep -qE "torch.OutOfMemoryError|CUDA error|EngineCore failed" /workspace/logs/vllm.log 2>/dev/null; then
  echo "FATAL"
  grep -E -m3 -B2 "OutOfMemoryError|CUDA error|EngineCore failed" /workspace/logs/vllm.log | tail -8
else
  tail -1 /workspace/logs/vllm.log 2>/dev/null | cut -c1-110
fi
REMOTE
  )
  printf '[%3d] %s\n' "$i" "$out"
  case "$out" in
    READY*) echo "vLLM is serving."; exit 0;;
    FATAL*) echo "vLLM hit a fatal error — see /workspace/logs/vllm.log"; exit 1;;
  esac
  sleep 30
done

echo "Still not serving after 90 minutes — check /workspace/logs/vllm.log"
exit 1
