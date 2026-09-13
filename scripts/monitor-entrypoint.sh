#!/bin/sh
set -eu

resolve_config() {
  _want="${CONFIG_PATH:-/app/monitor-config.yaml}"
  case "$_want" in
    /*) ;;
    *) _want="/app/$_want" ;;
  esac
  for _c in "$_want" /app/monitor-config.yaml /app/monitor-config/config.yaml; do
    if [ -f "$_c" ]; then
      printf '%s' "$_c"
      return 0
    fi
  done
  return 1
}

PORT="${MONITOR_PORT:-8090}"
case "$PORT" in
  ''|*[!0-9]*) PORT=8090 ;;
esac

if ! CFG="$(resolve_config)"; then
  echo "monitor: no config file found (tried CONFIG_PATH, /app/monitor-config.yaml, /app/monitor-config/config.yaml)" >&2
  echo "monitor: on the host: test -f config.yaml && docker compose config | grep -A3 monitor-config" >&2
  echo "monitor: or: ./scripts/setup-monitor-config.sh && docker compose up -d monitor" >&2
  exit 1
fi

if [ -d "$CFG" ]; then
  echo "monitor: $CFG is a directory inside the container." >&2
  echo "monitor: on the host: docker compose stop monitor && rm -rf config.yaml && cp config.example.yaml config.yaml" >&2
  exit 1
fi

if [ ! -d /app/monitor ] || [ ! -f /app/monitor/app.py ]; then
  echo "monitor: /app/monitor incomplete — rebuild: docker compose build --no-cache monitor" >&2
  exit 1
fi

if ! python3 -c "from monitor.app import app" 2>&1; then
  echo "monitor: failed to import monitor.app (see traceback above)" >&2
  exit 1
fi

echo "monitor: starting uvicorn on 0.0.0.0:${PORT} config=${CFG}" >&2
exec uvicorn monitor.app:app --host 0.0.0.0 --port "$PORT"
