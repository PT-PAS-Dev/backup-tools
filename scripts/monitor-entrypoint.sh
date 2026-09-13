#!/bin/sh
set -eu

readable_config() {
  [ -f "$1" ] && [ -r "$1" ]
}

resolve_config() {
  for _c in \
    /app/monitor-config/config.yaml \
    "${CONFIG_PATH:-}" \
    /app/monitor-config.yaml
  do
    [ -z "$_c" ] && continue
    case "$_c" in
      /*) ;;
      *) _c="/app/$_c" ;;
    esac
    if readable_config "$_c"; then
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
  echo "monitor: no readable config (expected /app/monitor-config/config.yaml)" >&2
  echo "monitor: on the host: cd .../backup-tools && ./scripts/setup-monitor-config.sh" >&2
  echo "monitor: SELinux: chmod 644 monitor-config/config.yaml; chcon -Rt container_file_t monitor-config" >&2
  exit 1
fi

if [ -d "$CFG" ]; then
  echo "monitor: $CFG is a directory inside the container." >&2
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
