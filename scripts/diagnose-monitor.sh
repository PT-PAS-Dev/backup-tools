#!/usr/bin/env bash
# Jalankan di host: ./scripts/diagnose-monitor.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== Host: config.yaml ==="
if [ -e config.yaml ]; then
  file config.yaml
  ls -la config.yaml
  head -3 config.yaml 2>/dev/null || true
else
  echo "MISSING: buat dengan cp config.example.yaml config.yaml"
fi

echo ""
echo "=== docker-compose (monitor mount) ==="
grep -E 'monitor-config|CONFIG_PATH|create_host_path' docker-compose.yml 2>/dev/null || echo "(grep gagal — cek docker-compose.yml manual)"

echo ""
echo "=== Container logs (tail 40) ==="
docker logs mariadb-clone-monitor --tail 40 2>&1 || echo "(container tidak ada)"

echo ""
echo "=== One-shot test in image ==="
docker compose run --rm --no-deps --entrypoint /bin/sh monitor -c '
set -eu
CFG="${CONFIG_PATH:-/app/monitor-config.yaml}"
echo "CONFIG_PATH=$CFG"
ls -la "$CFG" 2>&1 || true
if [ -d "$CFG" ]; then echo "RESULT: FAIL — config ter-mount sebagai folder"; exit 1
elif [ ! -f "$CFG" ]; then echo "RESULT: FAIL — bukan file"; exit 1
else echo "RESULT: OK — file config"; fi
echo "MONITOR_PORT=${MONITOR_PORT:-<unset>}"
python3 -c "from monitor.app import app; print(\"import OK\")"
'
