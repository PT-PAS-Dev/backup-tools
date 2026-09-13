#!/usr/bin/env bash
# Copy local secrets into the Docker volume (host reads files; avoids NFS bind-mount permission issues).
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -f config.yaml ]]; then
  echo "Missing config.yaml in $(pwd)" >&2
  exit 1
fi

docker compose up --no-start backup >/dev/null 2>&1 || true

volume="$(docker volume ls -q | grep '_backup-config$' | head -1)"
if [[ -z "$volume" ]]; then
  echo "Docker volume backup-config not found. Run: docker compose up -d --build" >&2
  exit 1
fi

docker run --rm -i -v "${volume}:/data" alpine:3.20 sh -c "cat > /data/config.yaml" < config.yaml

for file in oauth-client.json token.json; do
  if [[ -f "$file" ]]; then
    docker run --rm -i -v "${volume}:/data" alpine:3.20 sh -c "cat > /data/$file" < "$file"
  fi
done

if [[ -f token.json ]]; then
  docker run --rm -v "${volume}:/data" alpine:3.20 chmod 666 /data/token.json
fi

echo "Loaded config.yaml into volume ${volume}"
