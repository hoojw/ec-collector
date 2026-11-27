#!/usr/bin/env bash
set -euo pipefail
exec /opt/subsidy/run-ecdw-feeds.sh /opt/subsidy/config.json 9333 report analysis listed sales top3 subsidy aftersales
