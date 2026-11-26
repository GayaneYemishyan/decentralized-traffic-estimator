#!/usr/bin/env bash
set -euo pipefail
mkdir -p keys
ID1=$(go run . -key keys/node1.key -print-id)
go run . -key keys/node2.key -print-id >/dev/null
echo "NODE1_ID=$ID1" > .env
docker compose up --build