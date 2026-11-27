#!/usr/bin/env bash
set -u
config=${1:?config required}
port=${2:?port required}
shift 2
status=0
for feed in "$@"; do
  /opt/subsidy/venv/bin/python /opt/subsidy/direct_pipeline.py --config "$config" --port "$port" --feed "$feed" || status=1
done
exit "$status"
