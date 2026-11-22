# Bridge API (Python <-> Go), v0.1.0 — matches P3 PR #53 (`p2p-networking/bridge.go`)

Local HTTP server wrapping `Transport`. Default bind `127.0.0.1:8080` (Go flag `-bridge`, Python env `BRIDGE_ADDR`).
This document describes what the Go bridge **does**; where it differs from an earlier draft, the implementation won.

## Principles
1. **The bridge is opaque.** It moves bytes and never interprets envelope fields. Envelope schema, version, digest and
   origin checks live in Python (`serializer.parse_envelope`). The only checks in Go (`validPayload`): `1 <= len <= 65536`
   bytes and `json.Valid`.
2. **One topic per node**, fixed at start by `-topic` (default `traffic-state`). All nodes of a cluster must use the same one.
3. **Delivery is at-most-once, unordered, duplicates possible.** P4 de-duplicates on `message_id`.
4. **No echo:** a node's own publications are never returned by its own `/subscribe`.

## POST /publish
Request: `{"data": "<base64 of the envelope bytes>"}` (request body limit 96 KiB)

| Status | Meaning |
|---|---|
| 200 | `{"ok": true}`: accepted for broadcast (not a delivery guarantee) |
| 400 | `{"error": "..."}` bad body, bad base64, or payload empty / over 65536 bytes / not valid JSON |
| 405 | not a POST |
| 500 | libp2p publish failed; retryable |

## GET /subscribe?cursor=0&timeout_ms=25000&max=100
Long-poll over a ring buffer of the last 1024 messages.
- `cursor`: the **next cursor to read**. 0 or absent means 1. Treat it as opaque: start with 0, then always pass back `next_cursor`.
- `timeout_ms`: 0..30000, default 25000. `max`: 1..1024, default 100.
- Returns as soon as a message with cursor >= `cursor` exists, otherwise after `timeout_ms` with an empty list (still 200).

```json
{
  "boot_id": "9f1c2a7b3d4e5f60",
  "messages": [
    {"cursor": 17, "from": "12D3KooW...", "data": "<base64>"}
  ],
  "next_cursor": 18,
  "dropped": 0
}
```
- `next_cursor` = `cursor` + number of messages returned (equal to `cursor` when the list is empty).
- `dropped` > 0: that many messages left the ring buffer before this client read them.
- `from` is the libp2p peer ID of the message **author** (gossipsub signing makes it unforgeable), not of the forwarding hop.
- **`boot_id`** is random per bridge start. Cursors restart at 1 on a new boot, so when it changes the client must restart
  from cursor 0. The Python client does this automatically; it can take up to one `timeout_ms` to notice.
- A Python restart does not reset the bridge: the first poll (cursor 0) replays up to 1024 buffered messages, and `dropped`
  may be large if the ring has wrapped. Both are harmless because of de-duplication on `message_id`.

## GET /health
Always 200 once the HTTP server is up (it only starts after the libp2p host and pubsub exist):
`{"ready": true, "peer_id": "12D3KooW...", "peer_count": 3, "topic_peers": 2, "boot_id": "..."}`
- `ready` currently means "at least one peer is subscribed to the topic", so it is **false on a lone node**.
  The client therefore does not block on it: a node must work alone and keep its ledger (offline-first).
  Use `topic_peers` for connectivity. *(Open question for P3: keep `ready` as is, or make it mean "bridge up"?)*
- `peer_id` is this node's own libp2p ID (see Node identity).

## GET /peers
200 `{"peers": ["12D3KooW...", "..."]}`

## Node identity
`node_id` in every message is the libp2p peer ID (PR #53). Because `from` is authenticated, P4 enforces
`origin_node_id == from` (`serializer.check_peer_identity`) and drops spoofed origins. P2 reads its own ID from `/health`.

## Client behavior (P4, `orchestration/src/bridge_client.py`)
Per-request timeout 1 s (poll: `timeout_ms` + 2 s). Publish retries with exponential backoff and jitter. A circuit breaker
fails calls fast while the bridge is down so the ledger and trigger loop keep running. A retried publish can duplicate a
message whose first attempt succeeded but whose reply was lost; receivers de-duplicate on `message_id`.
