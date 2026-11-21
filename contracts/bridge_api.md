# Bridge API (Go node <-> Python), DRAFT: finalize in Week 1

Base URL: `http://127.0.0.1:8080` (configurable)

| Method | Path         | Body / Response                                             |
|--------|--------------|-------------------------------------------------------------|
| POST   | `/publish`   | Body: envelope (see `envelope_schema.json`). `202` on accept, `400` if invalid. |
| GET    | `/subscribe` | Server-Sent Events stream; each event `data:` is one envelope JSON. |
| GET    | `/health`    | `200 {"status":"ok","peers":<int>}`                          |

Open questions to settle in the contract-freeze issue:
- Subscribe transport: SSE vs long-poll
- Error body format
- Whether the node filters out its own messages
