# Contracts (maintainer: P4)

Single source of truth for everything crossing a module boundary. Current version: **0.2.0**.

| File | Direction | Producer -> consumer |
|---|---|---|
| `metrics_schema.json` | metrics | P1 -> P2 (via P4's microservice) |
| `state_schema.json` | inferred state | P2 -> P3/P4 |
| `envelope_schema.json` | peer envelope | P4 <-> P3 (Go carries the bytes opaquely) |
| `bridge_api.md` | Python <-> Go HTTP API | P3 <-> P4 |
| `examples/` | one valid example each; validated in CI (`scripts/validate_examples.py`) | |

## Versioning
- semver in `schema_version`. While major is 0, a **minor bump is breaking**; patch bumps are compatible.
- A node rejects messages whose version it does not speak, before any field validation.
- Any change = a Contract Change Proposal in a PR, approved by the affected pair, with updated examples.

## Protocol rules (no schema change)
- Only `scope: local` states are broadcast and aggregated (regional states stay on the node, no echo loops).
- Peer states are ordered per peer by envelope `created_at` (tie-break `seq`); `seq` resets on restart.
- Freshness (`max_age_s`) uses the receiver's `received_at`, never the peer's clock.
- `node_id` == libp2p peer ID; receivers enforce `origin_node_id == from`. The peer ID must never change (`-key`).
- State `schema_version` is the state contract version, not the metrics version.
- P1 -> P4 plumbing: one metrics JSON per line (NDJSON) on stdout; all logs on stderr.
- Triggers compare `congestion_score >= 0.80` (inclusive) with `confidence >= 0.65`; clear below `0.70`.
  Default scope is `local` so a single jammed node is not diluted by a regional average.
- Delivery is at-most-once and unordered; receivers de-duplicate on `message_id`.