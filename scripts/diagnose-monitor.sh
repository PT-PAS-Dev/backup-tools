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
CFG=/app/monitor-config/config.yaml
echo "CONFIG_PATH=${CONFIG_PATH:-}"
ls -la /app/monitor-config/ 2>&1 || true
if [ -r "$CFG" ]; then echo "RESULT: OK — readable config"; head -2 "$CFG"
elif [ -f "$CFG" ]; then echo "RESULT: FAIL — file ada tapi tidak bisa dibaca (SELinux?)"; exit 1
else echo "RESULT: FAIL — jalankan ./scripts/setup-monitor-config.sh di host"; exit 1
fi
echo "MONITOR_PORT=${MONITOR_PORT:-<unset>}"
python3 -c "from monitor.app import app; print(\"import OK\")"
'
