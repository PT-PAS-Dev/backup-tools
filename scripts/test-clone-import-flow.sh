#!/usr/bin/env bash
# Simulates docker clone import: sudo password must NOT appear in SQL fed to mariadb.
set -euo pipefail
cd "$(dirname "$0")/.."

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

DUMP_SQL="$WORK/dump.sql"
COMBINED_SQL="$WORK/combined.sql"
RECEIVED_SQL="$WORK/received.sql"
PW='@n0b1t4s4n'

printf '%s\n' "-- mini dump" "SELECT 'from-dump';" > "$DUMP_SQL"

build_combined() {
  printf '%s\n' 'SET SESSION innodb_strict_mode=0;' 'SET NAMES utf8mb4;' > "$COMBINED_SQL"
  cat "$DUMP_SQL" >> "$COMBINED_SQL"
}

FAKE="$WORK/fakebin"
mkdir -p "$FAKE"
cat > "$FAKE/sudo" <<'EOF'
#!/bin/bash
while [[ $# -gt 0 ]]; do
  case "$1" in
    -S|-p|'') shift ;;
    *) break ;;
  esac
done
read -r _sudo_pw
exec "$@"
EOF
chmod +x "$FAKE/sudo"
export PATH="$FAKE:$PATH"

build_combined

# Same pattern as monitor/clone.py import_bash (mariadb replaced by cat for test)
import_bash="cat < $(printf '%q' "$COMBINED_SQL") > $(printf '%q' "$RECEIVED_SQL")"

PW_B64=$(printf '%s' "$PW" | base64 | tr -d '\n')
PW_DECODED=$(printf '%s' "$PW_B64" | base64 -d)
echo "$PW_DECODED" | sudo -S bash -c "$import_bash"

if grep -q '@n0b1t4s4n' "$RECEIVED_SQL"; then
  echo "FAIL: sudo password leaked into SQL stream"
  cat "$RECEIVED_SQL"
  exit 1
fi
if ! grep -q 'innodb_strict_mode=0' "$RECEIVED_SQL"; then
  echo "FAIL: preamble missing from import"
  cat "$RECEIVED_SQL"
  exit 1
fi
if ! grep -q 'from-dump' "$RECEIVED_SQL"; then
  echo "FAIL: dump body missing from import"
  exit 1
fi

echo "OK: clone import flow — password not in SQL, preamble + dump present"
