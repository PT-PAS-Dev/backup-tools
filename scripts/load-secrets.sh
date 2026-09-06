#!/usr/bin/env bash
# Copy local secrets into the Docker volume (host reads files; avoids NFS bind-mount permission issues).
set -euo pipefail

cd "$(dirname "$0")/.."

for file in config.yaml oauth-client.json token.json; do
  if [[ ! -f "$file" ]]; then
    echo "Missing $file in $(pwd)" >&2
    exit 1
  fi
done

docker compose up --no-start backup >/dev/null 2>&1 || true

volume="$(docker volume ls -q | grep '_backup-config$' | head -1)"
if [[ -z "$volume" ]]; then
  echo "Docker volume backup-config not found. Run: docker compose up -d --build" >&2
  exit 1
fi

for file in config.yaml oauth-client.json token.json; do
  docker run --rm -i -v "${volume}:/data" alpine:3.20 sh -c "cat > /data/$file" < "$file"
done

docker run --rm -v "${volume}:/data" alpine:3.20 chmod 666 /data/token.json

echo "Loaded config.yaml, oauth-client.json, and token.json into volume ${volume}"
