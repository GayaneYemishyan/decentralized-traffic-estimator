#!/usr/bin/env bash
# Creates labels, milestones, and all project issues via GitHub CLI.
#
# Prereqs:  gh auth login   (and for the board: gh auth refresh -s project)
# Usage:    cd your-repo && ./setup_issues.sh
# Edit the 4 usernames below first. Leave one empty to skip assigning.

set -euo pipefail


P1="${P1:-viktoryamargaryan}"   # cv-module
P2="${P2:-Lilit862}"   # inference-module
P3="${P3:-GayaneYemishyan}"   # p2p-networking
P4="${P4:-Monika303}"   # orchestration + contracts

PROJECT_TITLE="Traffic Node Congestion Estimator"
CREATE_BOARD="${CREATE_BOARD:-true}"   # set true to also create a Project board

REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner)
echo "Repo: $REPO"

# ---------- Labels ----------
label() { gh label create "$1" --color "$2" --description "$3" --force >/dev/null; }
label cv-module        1d76db "Person 1: computer vision"
label inference-module 5319e7 "Person 2: inference / rule engine"
label p2p-networking   0e8a16 "Person 3: Go libp2p + bridge"
label orchestration    d93f0b "Person 4: validation, ledger, triggers, integration"
label contracts        fbca04 "Schemas and bridge API"
label week-1           c5def5 "Week 1 work"
label contract-change  b60205 "Change to a frozen contract: notify everyone"
label integration      006b75 "Cross-module integration"
label docs             0075ca "Documentation, demo, writeup"
label ci               bfdadc "Continuous integration"
echo "Labels done."

# ---------- Milestones ----------
milestone() {
  gh api "repos/$REPO/milestones" -f title="$1" -f description="$2" >/dev/null 2>&1 \
    || echo "  (milestone '$1' may already exist)"
}
M1="Week 1: Contract Freeze"
M2="Weeks 2-3: Modules"
M3="Week 4: Integration & Demo"
milestone "$M1" "Schemas and bridge API agreed and frozen"
milestone "$M2" "Each module works standalone against synthetic data"
milestone "$M3" "End-to-end demo, docs, polish"
echo "Milestones done."

# ---------- Optional project board ----------
PROJECT_NUM=""
if [ "$CREATE_BOARD" = "true" ]; then
  OWNER="${REPO%%/*}"
  PROJECT_NUM=$(gh project create --owner "$OWNER" --title "$PROJECT_TITLE" --format json -q .number)
  echo "Project board #$PROJECT_NUM created."
fi

# ---------- Issue helper ----------
# mk "title" "labels,comma,separated" "milestone" "assignee1,assignee2" "body"
mk() {
  local title="$1" labels="$2" ms="$3" assignees="$4" body="$5"
  local args=(--title "$title" --body "$body" --label "$labels" --milestone "$ms")
  [ -n "$assignees" ] && args+=(--assignee "$assignees")
  local url
  url=$(gh issue create "${args[@]}")
  echo "  $url"
  if [ -n "$PROJECT_NUM" ]; then
    gh project item-add "$PROJECT_NUM" --owner "${REPO%%/*}" --url "$url" >/dev/null
  fi
}

ALL=$(printf '%s\n' "$P1" "$P2" "$P3" "$P4" | grep -v '^$' | paste -sd, - || true)

echo "Creating issues..."

# =====================================================================
# WEEK 1: CONTRACT FREEZE
# =====================================================================
mk "Week 1: Freeze data contracts (metrics, state, envelope) & bridge API" \
   "contracts,week-1" "$M1" "$ALL" \
"## Goal
Agree on and freeze every interface between modules **before** feature branches start, so all four of us can build in parallel against the same contracts.

## Data flow
\`P1 (CV) -> metrics -> P2 (inference) -> state -> P3/P4 <-> envelope <-> P3 (p2p) <-> P4 (ledger)\`

## Checklist
- [ ] Finalize \`contracts/metrics_schema.json\` + example (\`P1 -> P2\`)
- [ ] Finalize \`contracts/state_schema.json\` + example (\`P2 -> P3/P4\`)
- [ ] Finalize \`contracts/envelope_schema.json\` + example (\`P3 <-> P4\`)
- [ ] Finalize \`contracts/bridge_api.md\` (local HTTP endpoints between Go and Python)
- [ ] Example JSON files committed under \`contracts/examples/\` (used as test fixtures)
- [ ] Every member has reviewed and approved in the PR

## Rules
- **Owner:** Person 4 (owns \`contracts/\`).
- Each schema PR needs approval from its **producer and consumer** (e.g. metrics: P1 + P2).
- After merge, contracts are **frozen**. Later changes go through a small PR labeled \`contract-change\` and must notify everyone.

## Done when
All four contract files and examples are merged to main, approved by all four members, and every example validates against its schema."

# =====================================================================
# PERSON 1: CV
# =====================================================================
mk "CV-1: Video replay mode & synthetic metrics generator" \
   "cv-module" "$M1" "$P1" \
"## Description
Do this first so P2-P4 are never blocked on YOLO. Provide (a) a replay mode that reads a video file as if it were a live feed, and (b) a synthetic generator that emits plausible metrics JSON without any model.

## Tasks
- [ ] Video file replay mode (configurable FPS, loop)
- [ ] Synthetic metrics generator with scenarios: free flow, rising congestion, sustained congestion, sudden jam
- [ ] CLI flags, e.g. \`--mode synthetic|replay --scenario ... --interval 1\`
- [ ] Output matches \`contracts/metrics_schema.json\`

## Depends on
Week 1 contract freeze (metrics schema).

## Done when
\`python -m cv_module --mode synthetic\` emits schema-valid JSON at the configured interval, and teammates are using it in their own modules."

mk "CV-2: YOLOv8-nano detection, ByteTrack tracking & line-crossing counts" \
   "cv-module" "$M2" "$P1" \
"## Description
Real vehicle detection and tracking on video, with per-lane or per-line counting.

## Tasks
- [ ] YOLOv8-nano vehicle detection (car, bus, truck, motorcycle)
- [ ] ByteTrack tracking with stable IDs
- [ ] Configurable counting line(s) and direction
- [ ] Per-interval counts and average occupancy/density
- [ ] Short benchmark note: FPS and accuracy on a sample clip

## Depends on
CV-1.

## Done when
On a sample video, line-crossing counts are within an agreed tolerance of a manual count, and the module runs at near real-time on a laptop."

mk "CV-3: Emit per-interval metrics JSON matching metrics_schema.json" \
   "cv-module" "$M2" "$P1" \
"## Description
Connect the real CV pipeline output to the frozen metrics contract.

## Tasks
- [ ] Aggregate CV-2 outputs into per-interval metrics
- [ ] Validate every emitted message against \`metrics_schema.json\`
- [ ] Same output interface as the synthetic generator (drop-in replacement)
- [ ] Handle dropped frames / empty intervals gracefully

## Depends on
CV-1, CV-2.

## Done when
Real and synthetic modes are interchangeable for P2, and 100% of emitted messages pass schema validation."

# =====================================================================
# PERSON 2: INFERENCE
# =====================================================================
mk "INF-1: Spatial-temporal grid ontology & rule engine (free/moderate/congested)" \
   "inference-module" "$M2" "$P2" \
"## Description
Map incoming metrics onto a spatial-temporal grid and classify each cell as free / moderate / congested using explicit rules.

## Tasks
- [ ] Define grid ontology (cell id, location, time window)
- [ ] Rule engine with configurable thresholds
- [ ] Consume metrics per \`metrics_schema.json\`
- [ ] Output state per \`state_schema.json\`
- [ ] Unit tests for rule boundaries

## Depends on
Week 1 contract freeze; CV-1 (synthetic data).

## Done when
Given the synthetic scenarios, the engine outputs the expected state for each, covered by unit tests."

mk "INF-2: Temporal smoothing filter to prevent state flickering" \
   "inference-module" "$M2" "$P2" \
"## Description
Prevent rapid free<->congested oscillation from noisy metrics.

## Tasks
- [ ] Smoothing (e.g. moving average, hysteresis, or minimum dwell time)
- [ ] Configurable window and thresholds
- [ ] Tests with noisy synthetic input showing reduced flicker
- [ ] Brief note on chosen method and parameters

## Depends on
INF-1.

## Done when
On a noisy synthetic scenario, state changes drop measurably versus the unsmoothed output, with a test proving it."

mk "INF-3: Peer-state aggregation & confidence scoring" \
   "inference-module" "$M2" "$P2" \
"## Description
Combine local state with states received from peer nodes and attach a confidence score.

## Tasks
- [ ] Aggregate local plus peer states per grid cell
- [ ] Confidence score (e.g. based on number of peers, freshness, agreement)
- [ ] Handle stale or conflicting peer data
- [ ] Output includes confidence per \`state_schema.json\`

## Depends on
INF-1, Week 1 contract freeze (envelope + state schemas).

## Done when
With simulated peer inputs (agreeing, conflicting, stale), aggregation and confidence behave as documented, covered by tests."

# =====================================================================
# PERSON 3: P2P
# =====================================================================
mk "P2P-1: Go libp2p host setup, peer discovery & pub/sub transport" \
   "p2p-networking" "$M2" "$P3" \
"## Description
Go service that creates a libp2p host, discovers peers, and exchanges messages over pub/sub.

## Tasks
- [ ] libp2p host with configurable port and identity
- [ ] Peer discovery (mDNS and/or bootstrap list)
- [ ] GossipSub topic for state envelopes
- [ ] Publish and receive envelopes per \`envelope_schema.json\`
- [ ] Logging of peers joining and leaving

## Depends on
Week 1 contract freeze (envelope schema).

## Done when
Two Go processes on one machine discover each other and exchange envelopes over pub/sub."

mk "P2P-2: Local HTTP bridge (/publish, /subscribe) for Python" \
   "p2p-networking" "$M2" "$P3" \
"## Description
Local HTTP API so the Python modules talk to the Go node without knowing about libp2p.

## Tasks
- [ ] Implement every endpoint in \`contracts/bridge_api.md\`
- [ ] \`POST /publish\` forwards an envelope to the pub/sub topic
- [ ] \`/subscribe\` delivers received envelopes (SSE, long-poll, or as specified)
- [ ] Basic error handling and a health endpoint
- [ ] curl examples in the README

## Depends on
P2P-1, \`contracts/bridge_api.md\`.

## Done when
A Python script can publish and receive envelopes purely through the bridge, and behavior matches \`bridge_api.md\`."

mk "P2P-3: Multi-node local test setup (Docker / multi-process) & join/leave handling" \
   "p2p-networking" "$M3" "$P3" \
"## Description
Reproducible way to run 3+ nodes locally, plus correct behavior when nodes join or leave.

## Tasks
- [ ] \`docker-compose.yml\` or a script launching N nodes with distinct ports and identities
- [ ] Nodes rejoin after restart
- [ ] Graceful handling of a node disappearing mid-run
- [ ] One-command startup documented in the README

## Depends on
P2P-1, P2P-2.

## Done when
\`docker compose up\` (or the script) starts 3 nodes that discover each other, and killing or restarting one does not crash the others."

# =====================================================================
# PERSON 4: ORCHESTRATION + CONTRACTS
# =====================================================================
mk "ORC-1: Schema validation & serialization module" \
   "orchestration,contracts" "$M1" "$P4" \
"## Description
Shared Python module that validates and serializes all messages, used by every other module.

## Tasks
- [ ] Load schemas from \`contracts/\`
- [ ] \`validate_metrics\`, \`validate_state\`, \`validate_envelope\`
- [ ] Serialize / deserialize helpers (canonical JSON for hashing)
- [ ] Clear error messages on failure
- [ ] Tests using \`contracts/examples/\` as fixtures

## Depends on
Week 1 contract freeze.

## Done when
All examples pass, deliberately broken payloads fail with readable errors, and other modules import and use it."

mk "ORC-2: SQLite append-only ledger with hash chaining" \
   "orchestration" "$M2" "$P4" \
"## Description
Tamper-evident local log of states and events.

## Tasks
- [ ] SQLite table: id, timestamp, payload, prev_hash, hash
- [ ] Append-only API (no update/delete exposed)
- [ ] Hash = SHA-256 of (prev_hash + canonical payload)
- [ ] \`verify_chain()\` detects tampering
- [ ] Tests: normal append, tampered row detected

## Depends on
ORC-1.

## Done when
Chain verification passes on clean data and fails when any row is altered, covered by tests."

mk "ORC-3: Congestion threshold triggers (>80%) & automated actions" \
   "orchestration" "$M2" "$P4" \
"## Description
Trigger automated actions when congestion exceeds the threshold.

## Tasks
- [ ] Configurable threshold (default 80%)
- [ ] Trigger logic with debounce so it does not fire repeatedly
- [ ] Pluggable actions (log, webhook, write alert to ledger)
- [ ] Record each trigger in the ledger
- [ ] Tests for crossing up, staying above, and dropping below

## Depends on
ORC-1, ORC-2, state schema.

## Done when
Feeding a rising-congestion scenario fires exactly one alert per crossing and logs it to the ledger."

mk "ORC-4: Microservice wrapper, bridge client & end-to-end integration/demo script" \
   "orchestration,integration" "$M3" "$P4" \
"## Description
Tie everything together: a service that wires CV -> inference -> bridge -> ledger/triggers, plus a demo script.

## Tasks
- [ ] Bridge client (Python) for \`/publish\` and \`/subscribe\`
- [ ] Service wrapper running ledger and trigger logic
- [ ] Demo script launching N nodes with synthetic data
- [ ] Documented expected output of the demo

## Depends on
ORC-1..3, P2P-2, INF-1, CV-1.

## Done when
One command runs a multi-node demo end to end, and the ledger and alerts show expected results."

# =====================================================================
# EXTRAS (recommended)
# =====================================================================
mk "INT-1: Two-node end-to-end smoke test with synthetic data" \
   "integration" "$M2" "$ALL" \
"## Description
Force early integration so mismatches show up in Week 2-3, not the final week.

## Checklist
- [ ] Node A and Node B run CV-1 synthetic -> inference -> bridge
- [ ] Each node receives the other's state and aggregates it
- [ ] Ledger entries written on both nodes
- [ ] Any contract problems found are fixed via \`contract-change\` PRs

## Done when
Scripted smoke test passes on a clean checkout."

mk "CI-1: Validate all example JSONs against their schemas in CI" \
   "ci,contracts" "$M1" "$P4" \
"## Description
Cheap guard against contract drift.

## Tasks
- [ ] GitHub Actions workflow on every PR
- [ ] Validate every file in \`contracts/examples/\` against its schema
- [ ] Fail the PR if a schema or example is changed without the other

## Done when
Workflow is required for merging into main."

mk "DOC-1: README, architecture diagram, demo script & writeup" \
   "docs" "$M3" "$P4" \
"## Description
Own the project documentation and final demo.

## Tasks
- [ ] README with setup, architecture diagram, and how to run the demo
- [ ] Per-module README sections (owners fill in their own)
- [ ] Demo walkthrough / presentation outline
- [ ] Final writeup

## Done when
A new person can clone the repo and run the demo using only the README."

echo "All issues created."
[ -n "$PROJECT_NUM" ] && echo "Board: set up columns (Backlog / In Progress / In Review / Done) in the Status field in the GitHub UI."
