#!/usr/bin/env bash
set -euo pipefail
exec /opt/subsidy/run-ecdw-feeds.sh "${STORE_CONFIG:?STORE_CONFIG required}" "${CHROME_PORT:?CHROME_PORT required}" report analysis listed sales top3 subsidy aftersales
