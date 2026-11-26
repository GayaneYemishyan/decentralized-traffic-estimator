#!/usr/bin/env bash
# Usage: scripts/run_local_cluster.sh [up|down] [extra docker compose args]
#   up   (default) build, derive node1's peer ID, write .env, start 3 nodes in the foreground
#   down stop the cluster (add -v to also wipe identities and ledgers)
# Tunables (env): SCENARIO_1..3 (free|rising|congested|jam), LANES, INTERVAL, TRIGGER_SCOPE
set -euo pipefail
cd "$(dirname "$0")/.."

cmd="${1:-up}"
[ $# -gt 0 ] && shift
export NODE1_ID="${NODE1_ID:-pending}"   # compose refuses to parse without it; replaced below

case "$cmd" in
  down) exec docker compose down "$@" ;;
  up) ;;
  *) echo "usage: $0 [up|down]" >&2; exit 64 ;;
esac

docker compose build
# Loads or creates /keys/node1.key (named volume) and prints the peer ID on stdout.
NODE1_ID="$(docker compose run --rm --no-deps -T go1 -key /keys/node1.key -print-id | tr -d '\r\n')"
if ! [[ "$NODE1_ID" =~ ^12D3KooW[1-9A-HJ-NP-Za-km-z]+$ ]]; then
  echo "unexpected peer ID from -print-id: '$NODE1_ID'" >&2
  exit 1
fi
export NODE1_ID
echo "NODE1_ID=$NODE1_ID" > .env
echo "node1 peer ID: $NODE1_ID"

exec docker compose up "$@"