#!/usr/bin/env bash
# DOC-1 demo: 3 nodes -> jam -> alert -> tamper -> detection. Needs Docker. Ctrl-C safe.
set -euo pipefail
cd "$(dirname "$0")/.."
step() { printf '\n== %s\n' "$*"; }
mkdir -p .demo

step "1/6 start 3 nodes (scenario: jam)"
scripts/run_local_cluster.sh up -d

step "2/6 wait for gossip + jam, check the acceptance table on the live cluster"
python3 scripts/integration_test.py --cluster-only || true

step "3/6 automated actions on node1"
docker compose exec -T node1 cat /data/actions.jsonl || echo "(no actions yet)"

step "4/6 anchor the ledger head outside the container"
docker compose exec -T node1 python /app/scripts/ledger_anchor.py save --db /data/ledger.db >> .demo/anchors.jsonl
tail -n 1 .demo/anchors.jsonl
docker compose exec -T node1 python /app/scripts/ledger_anchor.py verify --db /data/ledger.db --anchors - < .demo/anchors.jsonl

step "5/6 attacker with file access removes the trigger and edits an entry"
docker compose exec -T node1 python - <<'PY'
import json, sqlite3
c = sqlite3.connect("/data/ledger.db", isolation_level=None, timeout=10)
c.execute("DROP TRIGGER entries_no_update")
seq, p = c.execute("SELECT seq, payload FROM entries WHERE entry_type='state' ORDER BY seq LIMIT 1").fetchone()
d = json.loads(p); d["seq"] += 1000
c.execute("UPDATE entries SET payload=? WHERE seq=?", (json.dumps(d, sort_keys=True, separators=(",", ":")), seq))
print("tampered entry", seq)
PY

step "6/6 verification catches it"
docker compose exec -T node1 python /app/scripts/ledger_anchor.py verify --db /data/ledger.db --anchors - < .demo/anchors.jsonl || true
echo; echo "cleanup: scripts/run_local_cluster.sh down -v"