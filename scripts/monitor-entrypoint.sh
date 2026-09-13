#!/bin/sh
set -eu

CFG="${CONFIG_PATH:-/app/monitor-config.yaml}"
case "$CFG" in
  /*) ;;
  *) CFG="/app/$CFG" ;;
esac

if [ -d "$CFG" ]; then
  echo "monitor: $CFG is a directory inside the container." >&2
  echo "monitor: on the host: docker compose stop monitor && rm -rf config.yaml && cp config.example.yaml config.yaml && nano config.yaml" >&2
  exit 1
fi

if [ ! -f "$CFG" ]; then
  echo "monitor: missing config file at $CFG (CONFIG_PATH=${CONFIG_PATH:-})" >&2
  echo "monitor: on the host: test -f config.yaml || cp config.example.yaml config.yaml" >&2
  exit 1
fi

if [ ! -d /app/monitor ] || [ ! -f /app/monitor/app.py ]; then
  echo "monitor: /app/monitor incomplete — rebuild: docker compose build --no-cache monitor" >&2
  exit 1
fi

exec uvicorn monitor.app:app --host 0.0.0.0 --port "${MONITOR_PORT:-8090}"
