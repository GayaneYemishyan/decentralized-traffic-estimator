# Bridge API (Python <-> Go), v0.1.0 — DRAFT for P3 review

Local HTTP server in `p2p-networking/bridge.go`. Bound to loopback only.
Default address `127.0.0.1:8080` (Python reads `BRIDGE_ADDR`). The bridge never parses `data`: it is opaque bytes.

Delivery semantics: **at-most-once, unordered, duplicates possible.** P4 de-duplicates on `message_id`.
The bridge must **not** deliver a node's own publications back to its own `/subscribe`.

## POST /publish
Request (`application/json`):
`{"topic": "traffic/state", "data": "<base64 of envelope bytes>"}`

| Status | Meaning |
|---|---|
| 200 | `{"status":"queued"}` accepted for broadcast (not a delivery guarantee) |
| 400 | `{"error":"..."}` bad JSON, bad base64, or unknown topic |
| 413 | `data` larger than 64 KiB (decoded) |
| 503 | libp2p host not ready; safe to retry |

## GET /subscribe?topic=traffic/state&cursor=0&timeout_ms=5000&max=100
Long-poll. Returns as soon as one message newer than `cursor` exists, otherwise after `timeout_ms` (empty list, still 200).
The bridge keeps a ring buffer of the last 1024 messages per topic, so a dropped HTTP connection loses nothing.

```json
{
  "messages": [
    {"cursor": 17, "from_peer": "12D3Koo...", "received_at": "2026-11-20T10:00:10.310Z", "data": "<base64>"}
  ],
  "next_cursor": 17,
  "dropped": 0
}
```
`cursor` is a per-topic monotonically increasing integer. `dropped` > 0 means messages fell out of the ring buffer before they were read.
`from_peer` is the libp2p peer ID. It is NOT the contract `node_id`; the envelope is authoritative.

## GET /health
200 `{"ready": true, "peer_count": 3}`. Python uses this for startup gating and circuit-breaker probing.

## GET /peers
200 `{"peers": ["12D3Koo...", "..."]}`

## Client behavior (P4, `bridge_client.py`)
Timeouts: connect 1 s, read `timeout_ms` + 2 s. Retries with exponential backoff and jitter (max 5 s). A bridge outage must never stop the local ledger/trigger loop.
