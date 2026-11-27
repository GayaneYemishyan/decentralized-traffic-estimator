# p2p-networking

Go libp2p node (GossipSub) plus a local HTTP bridge so the Python modules can broadcast and receive opaque messages. This layer is transport only: Go checks size and well-formed JSON, and all schema, version, digest and origin checks happen in Python (`serializer.parse_envelope`).

## Run

Single node:

```
go run . -key keys/node1.key -bridge 127.0.0.1:8080
```

Local 3-node cluster (Docker): `./run_local_cluster.sh` (bash) or `powershell -ExecutionPolicy Bypass -File .\run_local_cluster.ps1` (Windows). Bridges are exposed on `127.0.0.1:8080` (node1), `127.0.0.1:8081` (node2) and `127.0.0.1:8082` (node3).

| Flag | Default | Meaning |
|---|---|---|
| `-key` | empty (random identity) | identity key file, created if missing |
| `-port` | `4001` | libp2p TCP port |
| `-bootstrap` | empty | multiaddr of a bootstrap peer (re-dialed with backoff) |
| `-bridge` | `127.0.0.1:8080` | HTTP bridge bind address |
| `-topic` | `traffic-state` | pub/sub topic |
| `-print-id` | false | print the peer ID for `-key` and exit |

Peers on the same machine find each other via mDNS; in Docker use `-bootstrap`. 
In Docker, node2 and node3 bootstrap only to node1 (star topology). Messages between them are relayed through node1, so if node1 is down they are isolated until it returns, because mDNS does not work inside Docker networks.

## Message rules

- Payload is any valid JSON, **1 to 65536 bytes**.
- Over HTTP the payload is **base64** inside a JSON wrapper.
- A node does **not** receive its own messages. Only messages from other peers appear in `/subscribe`.
- Delivery is best effort and at most once. Nothing is persisted; the bridge keeps the last 1024 received messages in memory.

## HTTP API

The bridge has no authentication. Keep it bound to localhost.

### `POST /publish`

Request:

```json
{"data": "<base64 of the JSON payload>"}
```

| Status | Body | When |
|---|---|---|
| 200 | `{"ok": true}` | published to the topic |
| 400 | `{"error": "..."}` | bad request body, body over 96 KiB, invalid base64, payload outside 1..65536 bytes, or payload not valid JSON |
| 405 | `{"error": "POST only"}` | any method other than POST |
| 500 | `{"error": "..."}` | libp2p publish failed |

A 200 with no other peers connected still succeeds; the message just has no one to reach. Check `topic_peers` in `/health`.

### `GET /subscribe`

Long-poll for messages received from other peers.

| Query param | Default | Notes |
|---|---|---|
| `cursor` | `1` | first cursor you want; values below 1 are treated as 1 |
| `max` | `100` | max messages per response, capped at 1024 |
| `timeout_ms` | `25000` | how long to wait if nothing is new, capped at 30000; `0` returns immediately |

Response:

```json
{
  "boot_id": "173d5e1764325be2",
  "messages": [{"cursor": 1, "from": "12D3KooW...", "data": "<base64>"}],
  "next_cursor": 2,
  "dropped": 0
}
```

Cursor semantics:

- Every received message gets an increasing cursor starting at 1 for each node run.
- Always pass the returned `next_cursor` as the next `cursor`. If nothing arrives before the timeout, `messages` is empty and `next_cursor` equals the cursor you sent.
- **`dropped`**: if you fall more than 1024 messages behind, the oldest are overwritten. The response starts at the oldest message still held and `dropped` is the number you missed.
- **`boot_id`**: random per node run. If it changes, the node restarted and its cursors reset to 1, so your old cursor is meaningless (the poll would just wait forever). Reset your cursor to 1.

### `GET /health`

```json
{"ready": true, "peer_id": "12D3KooW...", "peer_count": 1, "topic_peers": 1, "boot_id": "..."}
```

`ready` only means the bridge is serving. Use `topic_peers` (peers subscribed to our topic) to know whether broadcasts can reach anyone.

### `GET /peers`

```json
{"peers": ["12D3KooW...", "..."]}
```

All connected libp2p peers.

## curl examples

```bash
# publish on node1
B64=$(printf '%s' '{"node":"n1","congestion":0.7}' | base64 | tr -d '\n')
curl -s -X POST http://127.0.0.1:8080/publish -d "{\"data\":\"$B64\"}"

# receive on node2 (long-poll, up to 5 s)
curl -s "http://127.0.0.1:8081/subscribe?cursor=1&timeout_ms=5000"

# health and peers
curl -s http://127.0.0.1:8080/health
curl -s http://127.0.0.1:8080/peers
```

## Python client

`client.py` wraps the API (standard library only, Python 3.9+):

```python
from client import BridgeClient

c = BridgeClient("http://127.0.0.1:8080")
c.on_message(lambda from_peer, data: print(from_peer, data))   # data is raw bytes
c.on_dropped(lambda n: print("missed", n))                      # optional
c.start()                                                       # background long-poll thread
c.publish({"node": "n1", "congestion": 0.7})                    # dict/list -> JSON; bytes/str sent as-is
c.stop()
```

The client follows `next_cursor`, resets its cursor when `boot_id` changes, retries with backoff if the bridge is down, and starts at cursor 1, so it replays whatever is still in the ring. `publish` raises `BridgeError` on rejection or if the bridge is unreachable.

## Tests

```
go test ./...                 # validPayload, transport (2 in-process hosts), bridge (httptest)
```

Smoke scripts that need the running cluster: `bridge_smoke.py` (oversize rejection, ring overflow) and `validate_smoke.py` (publish validation).

## Join/leave

Peer joins and leaves are logged (`peer joined: <id>` / `peer left: <id>`). A bootstrapped node re-dials its bootstrap peer automatically after a disconnect. To observe it:

```
docker compose stop node2     # topic_peers on node1 -> 0
docker compose start node2    # topic_peers on node1 -> 1
```