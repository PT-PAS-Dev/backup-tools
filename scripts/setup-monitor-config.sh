#!/usr/bin/env bash
# Salin config ke monitor-config/ (fallback mount) sebelum docker compose up monitor
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
file monitor-config/config.yaml
