#!/usr/bin/env bash
set -euo pipefail

instance=${1:?browser instance is required}
shift
: "${CHROME_PORT:?CHROME_PORT is required}"
(( $# > 0 )) || { echo "collector command is required" >&2; exit 2; }
[[ "$instance" =~ ^[a-z][a-z0-9-]*-E$ ]] || { echo "invalid Meituan ecommerce browser instance: $instance" >&2; exit 2; }
[[ "$CHROME_PORT" =~ ^[0-9]+$ ]] || { echo "invalid Chrome port: $CHROME_PORT" >&2; exit 2; }

lock_file="/run/lock/meituan-browser-${instance}.lock"
exec 9>"$lock_file"
/usr/bin/flock -w 60 9

cleanup() {
  /usr/bin/systemctl stop "meituan-store-chrome@${instance}.service" >/dev/null 2>&1 || true
}
trap cleanup EXIT

/usr/bin/systemctl start "meituan-store-chrome@${instance}.service"
ready=0
for _ in {1..60}; do
  if /usr/bin/curl --fail --silent "http://127.0.0.1:${CHROME_PORT}/json/version" >/dev/null; then
    ready=1
    break
  fi
  /usr/bin/sleep 1
done
(( ready == 1 )) || { echo "Meituan Chrome is not ready: instance=$instance port=$CHROME_PORT" >&2; exit 1; }

# A retained Meituan session can first arrive at the login page.  The helper
# presses the submit control, but only after Chrome has itself populated the
# password field (length-only check); it never types or reads credentials.
# A still-pending login is surfaced by the collectors, so a failure here must
# not abort the cycle.
app_dir=${APP_DIR:-/opt/subsidy}
recovery_python=${RECOVERY_PYTHON:-/opt/subsidy/venv/bin/python}
if [[ -x "$recovery_python" && -f "$app_dir/login_recovery.py" ]]; then
  "$recovery_python" "$app_dir/login_recovery.py" \
    --platform meituan --port "$CHROME_PORT" --settle-seconds 12 || true
fi

status=0
"$@" || status=$?
exit "$status"
