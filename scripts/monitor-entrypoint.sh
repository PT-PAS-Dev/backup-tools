#!/bin/sh
set -eu

CFG="/app/config/config.yaml"

if [ ! -e "$CFG" ]; then
  echo "monitor: missing $CFG — copy config.example.yaml to config.yaml on the host" >&2
  exit 1
fi

if [ -d "$CFG" ]; then
  echo "monitor: $CFG is a directory (Docker created it because the file was missing)." >&2
  echo "monitor: on the host run: rm -rf config.yaml && cp config.example.yaml config.yaml" >&2
  exit 1
fi

if [ ! -d /app/monitor ] || [ ! -f /app/monitor/app.py ]; then
  echo "monitor: /app/monitor incomplete — rebuild: docker compose build --no-cache monitor" >&2
  exit 1
fi

exec uvicorn monitor.app:app --host 0.0.0.0 --port "${MONITOR_PORT:-8090}"
