#!/usr/bin/env bash
# Step 5 — end all billing. `stop` does NOT: stopped instances keep charging
# for their disk. Only `destroy` releases the storage.
set -euo pipefail
: "${VAST_API_KEY:?export VAST_API_KEY first}"
INSTANCE_ID="${1:?usage: 05-destroy.sh <INSTANCE_ID>}"
vastai destroy instance "$INSTANCE_ID"
vastai show instances --raw | jq -r '.[] | "STILL RUNNING: \(.id) $\(.dph_total)/hr"'
echo "If nothing printed above, you are no longer being billed."
