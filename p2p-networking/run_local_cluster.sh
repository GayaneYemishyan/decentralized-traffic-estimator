#!/usr/bin/env bash
set -euo pipefail

# Work from the p2p-networking directory whether this script lives there or in scripts/.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$HERE/main.go" ]; then
  cd "$HERE"
else
  cd "$HERE/../p2p-networking"
fi

mkdir -p keys
ID1=$(go run . -key keys/node1.key -print-id)
go run . -key keys/node2.key -print-id >/dev/null
echo "NODE1_ID=$ID1" > .env
docker compose up --build