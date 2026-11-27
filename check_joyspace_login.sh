#!/usr/bin/env bash
set -euo pipefail

if systemctl is-active --quiet joyspace-subsidy-sync.service; then
  echo '{"logged_in":false,"reason":"JoySpace subsidy sync is running"}' >&2
  exit 3
fi

browser_was_active=false
if systemctl is-active --quiet joyspace-browser.service; then
  browser_was_active=true
else
  systemctl start joyspace-browser.service
fi

cleanup() {
  if [[ "$browser_was_active" != true ]]; then
    systemctl --no-block stop joyspace-browser.service
  fi
}
trap cleanup EXIT

for _ in $(seq 1 60); do
  if curl --fail --silent --max-time 2 http://127.0.0.1:9533/json/list \
    | grep -q 'joyspace.jd.com/sheets/'; then
    /opt/subsidy/venv/bin/python /opt/subsidy/check_joyspace_login.py --port 9533
    exit $?
  fi
  sleep 2
done

echo '{"logged_in":false,"reason":"JoySpace sheet page was not ready"}' >&2
exit 2
