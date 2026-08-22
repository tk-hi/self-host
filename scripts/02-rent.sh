#!/usr/bin/env bash
# Step 2 — rent one offer. Run only after you have picked an offer id.
#   ./02-rent.sh <OFFER_ID>
set -euo pipefail

: "${VAST_API_KEY:?export VAST_API_KEY first}"
OFFER_ID="${1:?usage: 02-rent.sh <OFFER_ID>}"
VASTAI="${VASTAI:-vastai}"
DISK="${DISK:-100}"
WEBUI_PORT="${WEBUI_PORT:-8080}"
# A CUDA image with docker-in-docker available. The stack itself runs as
# compose *inside* this instance, so the template must permit nested docker.
IMAGE="${IMAGE:-vastai/base-image:cuda-12.4.1-auto}"

echo "Renting offer ${OFFER_ID}: ${DISK}GB disk, publishing only ${WEBUI_PORT}"

"$VASTAI" create instance "$OFFER_ID" \
  --image "$IMAGE" \
  --disk "$DISK" \
  --ssh --direct \
  --env "-p ${WEBUI_PORT}:${WEBUI_PORT}" \
  --onstart-cmd 'touch /workspace/.provisioned' \
  --raw | tee /tmp/vast-create.json

INSTANCE_ID=$(jq -r '.new_contract' /tmp/vast-create.json)
echo "INSTANCE_ID=${INSTANCE_ID}"
echo "Waiting for it to reach 'running' (image pull can take a few minutes)..."

for _ in $(seq 1 120); do
  status=$("$VASTAI" show instance "$INSTANCE_ID" --raw | jq -r '.actual_status // "pending"')
  echo "  status=${status}"
  [ "$status" = "running" ] && break
  sleep 15
done

"$VASTAI" show instance "$INSTANCE_ID" --raw \
  | jq '{id, actual_status, public_ipaddr, ssh_host, ssh_port, dph_total, ports}'
echo
echo "Public URL will be http://<public_ipaddr>:<external port mapped to ${WEBUI_PORT}>"
