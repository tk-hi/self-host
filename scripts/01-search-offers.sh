#!/usr/bin/env bash
# Step 1 — find rentable offers. Prints the top candidates as a table so you
# can pick one before anything is billed. Nothing here rents.
set -euo pipefail

: "${VAST_API_KEY:?export VAST_API_KEY first}"
VASTAI="${VASTAI:-vastai}"
DISK="${DISK:-100}"   # GB — ~30GB of weights plus images and headroom

query_common="verified=true rentable=true datacenter=true disk_space>=${DISK} inet_down>=200 reliability>0.98"

show() {
  local label="$1"; shift
  echo
  echo "=== ${label} ==="
  # shellcheck disable=SC2086
  "$VASTAI" search offers $* --raw \
    | jq -r --arg d "$DISK" '
        sort_by(.dph_total)[:5][]
        | [ (.id|tostring),
            .gpu_name,
            ((.num_gpus|tostring) + "x"),
            ((.gpu_ram/1024|floor|tostring) + "GB"),
            ("$" + (.dph_total|tostring) + "/hr"),
            ("rel " + (.reliability2*100|floor|tostring) + "%"),
            ((.inet_down|floor|tostring) + "/" + (.inet_up|floor|tostring) + " Mbps"),
            ("disk $" + (.storage_cost|tostring) + "/GB/mo"),
            .geolocation
          ] | @tsv' \
    | column -t -s $'\t'
}

show "RTX 4090 24GB (4-bit AWQ)" "gpu_name=RTX_4090 num_gpus=1 ${query_common}"
show "A100 80GB (FP8, only worth it under \$1/hr)" "gpu_name=A100_SXM4 num_gpus=1 gpu_ram>=80000 dph_total<1.0 ${query_common}"
show "A100 80GB PCIE" "gpu_name=A100_PCIE num_gpus=1 gpu_ram>=80000 dph_total<1.0 ${query_common}"

cat <<'NOTE'

Reading the table:
  $/hr here is the GPU rental only. Add storage (disk $/GB/mo x your disk size)
  and bandwidth. Storage keeps billing while the instance is merely STOPPED --
  only `destroy` ends it.
NOTE
