#!/usr/bin/env bash
# Wajib sebelum: docker compose up monitor
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p monitor-config
if [ -f config.yaml ]; then
  cp -f config.yaml monitor-config/config.yaml
  echo "Copied config.yaml -> monitor-config/config.yaml"
elif [ ! -f monitor-config/config.yaml ]; then
  cp config.example.yaml monitor-config/config.yaml
  echo "Created monitor-config/config.yaml from config.example.yaml — edit before production use."
else
  echo "monitor-config/config.yaml already exists."
fi
chmod 644 monitor-config/config.yaml
if command -v chcon >/dev/null 2>&1 && [ "$(getenforce 2>/dev/null || echo Disabled)" != "Disabled" ]; then
  chcon -Rt container_file_t monitor-config 2>/dev/null \
    || chcon -Rt svirt_sandbox_file_t monitor-config 2>/dev/null \
    || echo "Note: chcon gagal — jika container Permission denied, jalankan sebagai root: chcon -Rt container_file_t monitor-config"
fi
file monitor-config/config.yaml
ls -la monitor-config/config.yaml
