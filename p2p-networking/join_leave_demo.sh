#!/usr/bin/env bash
# Join/leave demo for P2P-3. Start the cluster first: ./run_local_cluster.sh
# Stops node2, restarts it, and checks topic_peers on node1 goes 1 -> 0 -> 1 and
# that messages flow again after the rejoin. Exits non-zero on failure.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

NODE1=http://127.0.0.1:8080
NODE2=http://127.0.0.1:8081

topic_peers() { curl -sf "$1/health" | grep -o '"topic_peers":[0-9]*' | cut -d: -f2; }

wait_for() { # wait_for <expected> <timeout_seconds>
  local want=$1 deadline=$((SECONDS + $2)) got="?"
  while [ "$SECONDS" -lt "$deadline" ]; do
    got=$(topic_peers "$NODE1" || echo "?")
    [ "$got" = "$want" ] && return 0
    sleep 1
  done
  echo "FAIL: node1 topic_peers did not reach $want (last: $got)"
  exit 1
}

T0=$(date -u +%Y-%m-%dT%H:%M:%SZ)

wait_for 1 30
echo "before : node1 topic_peers=$(topic_peers "$NODE1")"

docker compose stop node2
wait_for 0 30
echo "stopped: node1 topic_peers=$(topic_peers "$NODE1")"

docker compose start node2
wait_for 1 60
echo "started: node1 topic_peers=$(topic_peers "$NODE1")"

# Messages must flow again after the rejoin (node2's bridge restarted, so its ring is empty).
B64=$(printf '%s' '{"demo":"rejoin"}' | base64 | tr -d '\n')
curl -sf -X POST "$NODE1/publish" -d "{\"data\":\"$B64\"}" >/dev/null
if curl -sf "$NODE2/subscribe?cursor=1&timeout_ms=5000" | grep -q "$B64"; then
  echo "message after rejoin: received on node2"
else
  echo "FAIL: node2 did not receive the message after rejoining"
  exit 1
fi

echo "--- node1 join/leave log lines"
docker compose logs --since "$T0" --no-log-prefix node1 | grep -E 'peer (joined|left)' || true
echo "PASS"
