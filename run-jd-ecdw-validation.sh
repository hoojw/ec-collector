#!/usr/bin/env bash
set -euo pipefail
: "${BROWSER_INSTANCE:?}" "${CHROME_PORT:?}" "${STORE_KEY:?}"
[[ "$BROWSER_INSTANCE" =~ ^[a-z][a-z0-9-]*-E$ ]] || exit 2
exec 9>"/run/lock/jd-store-cycle-${STORE_KEY}.lock"
flock -n 9 || exit 75
cleanup() { systemctl stop "jingdong-store-chrome@${BROWSER_INSTANCE}.service" >/dev/null 2>&1 || true; }
trap cleanup EXIT
systemctl start "jingdong-store-chrome@${BROWSER_INSTANCE}.service"
for _ in {1..40}; do
 if curl -fsS "http://127.0.0.1:${CHROME_PORT}/json/version" >/dev/null 2>&1; then break; fi
 sleep 1
done
/opt/subsidy/venv/bin/python /opt/subsidy/jd_login_page_recover.py --port "$CHROME_PORT" --settle-seconds 12
if (( $# == 0 )); then set -- dashboard orders analysis sales; fi
/opt/subsidy/run-ecdw-feeds.sh "$STORE_CONFIG" "$CHROME_PORT" "$@"
