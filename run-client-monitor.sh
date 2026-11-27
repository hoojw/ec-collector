#!/usr/bin/env bash
set -uo pipefail

client_key=${CLIENT_KEY:?CLIENT_KEY is required}
client_units=${CLIENT_UNITS:?CLIENT_UNITS is required}
read -r -a units <<< "$client_units"

(( ${#units[@]} >= 2 )) || { echo "$client_key requires at least two platform units" >&2; exit 2; }
for unit in "${units[@]}"; do
  if [[ ! "$unit" =~ ^(jd-store-cycle@jd-[0-9]+|meituan-after-sales-pending|meituan-baientang-ops|meituan-store-ops@[a-z][a-z0-9-]*-E)\.service$ ]]; then
    echo "unsupported client platform unit: $unit" >&2
    exit 2
  fi
done

echo "$client_key starting platform units in parallel: ${units[*]}"
systemctl reset-failed "${units[@]}" >/dev/null 2>&1 || true

# A single systemctl transaction enqueues every unit before waiting, so the
# platform services run concurrently.  Each child owns its browser lifecycle
# and database failure policy.
start_status=0
systemctl start "${units[@]}" || start_status=$?

failed=0
for unit in "${units[@]}"; do
  active_state="$(systemctl show "$unit" -p ActiveState --value)"
  sub_state="$(systemctl show "$unit" -p SubState --value)"
  result="$(systemctl show "$unit" -p Result --value)"
  echo "$client_key platform result: unit=$unit active=$active_state sub=$sub_state result=$result"
  if [[ "$active_state" != "inactive" || "$result" != "success" ]]; then
    failed=1
  fi
done

(( start_status == 0 && failed == 0 ))
