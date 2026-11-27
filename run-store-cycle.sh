#!/usr/bin/env bash
# Per-store cross-platform collection cycle.  The two platform branches run in
# parallel, while a two-slot semaphore protects the 8 GB VM from too many
# simultaneous Chrome instances.
set -uo pipefail

store=${1:?store id is required}
jd_key=${JD_STORE_KEY:?JD_STORE_KEY is required}
mt_unit=${MT_UNIT:?MT_UNIT is required}
label=${STORE_LABEL:-$store}

[[ "$store" =~ ^[a-z][a-z0-9-]*$ ]] || { echo "invalid store id: $store" >&2; exit 2; }
[[ "$jd_key" =~ ^jd-[0-9]+$ ]] || { echo "invalid JD store key: $jd_key" >&2; exit 2; }
[[ "$mt_unit" =~ ^(meituan-after-sales-pending|meituan-baientang-ops|meituan-store-ops@[a-z][a-z0-9-]*-E)\.service$ ]] \
  || { echo "invalid Meituan unit: $mt_unit" >&2; exit 2; }

log_dir=/var/log/subsidy/store-cycle
install -d -m 0750 "$log_dir"

exec 23>/run/lock/store-cycle-slot0.lock
exec 24>/run/lock/store-cycle-slot1.lock
if ! /usr/bin/flock -n 23 && ! /usr/bin/flock -n 24; then
  echo "$label: both store-cycle slots are busy; waiting for slot 0" >&2
  /usr/bin/flock -w 1800 23 || {
    echo "$label: timed out waiting for a store-cycle slot" >&2
    exit 1
  }
fi

ts=$(TZ=Asia/Shanghai date +%F-%H%M%S)
# Ubuntu 22.04's journalctl does not accept GNU date's ISO string with a
# ``T`` separator (for example 2026-09-22T09:45:47+08:00).  Use its native
# local timestamp syntax so log export cannot silently degrade to an error.
journal_since=$(TZ=Asia/Shanghai date '+%Y-%m-%d %H:%M:%S')
jd_log="$log_dir/${store}-jd-${ts}.log"
mt_log="$log_dir/${store}-mt-${ts}.log"
echo "$label: starting cross-platform cycle (jd=$jd_key meituan=$mt_unit)"

jd_rc=0
mt_rc=0
/usr/bin/systemctl start "jd-store-cycle@${jd_key}.service" >"$jd_log" 2>&1 &
jd_job=$!
/usr/bin/systemctl start "$mt_unit" >"$mt_log" 2>&1 &
mt_job=$!

wait "$jd_job" || jd_rc=$?
wait "$mt_job" || mt_rc=$?

# systemctl start itself is normally silent.  Export the actual unit journals
# after each run so these per-cycle files contain actionable collector output.
/usr/bin/journalctl -u "jd-store-cycle@${jd_key}.service" \
  --since "$journal_since" --no-pager >>"$jd_log" 2>&1 || true
/usr/bin/journalctl -u "$mt_unit" \
  --since "$journal_since" --no-pager >>"$mt_log" 2>&1 || true

# Ecommerce stages commit directly; no legacy ETL is called.

if (( jd_rc != 0 || mt_rc != 0 )); then
  echo "$label store cycle partial failure: jd=${jd_rc} meituan=${mt_rc}" >&2
  exit 1
fi

echo "$label store cycle finished: jd=${jd_rc} meituan=${mt_rc}"
