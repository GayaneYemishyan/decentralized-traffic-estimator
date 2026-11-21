# Contracts

Maintainer: P4 (Monika). Any change needs PR approval from the affected pair.

| File | Direction | Version |
|---|---|---|
| metrics_schema.json | P1 -> P2 | 0.1.0 (draft) |
| state_schema.json | P2 -> P3/P4 | 0.1.0 (draft) |
| envelope_schema.json | P3 <-> P4 | 0.1.0 (draft) |
| bridge_api.md | Python <-> Go | 0.1.0 (draft) |

## Versioning
`MAJOR.MINOR.PATCH`. While MAJOR is 0, a MINOR bump is breaking. Patch = docs/clarifications only. Receivers reject incompatible versions loudly (`UnsupportedVersionError`).

## Conventions
- Timestamps: UTC RFC 3339 with exactly milliseconds, `YYYY-MM-DDTHH:MM:SS.mmmZ`.
- IDs (`node_id`, `lane_id`): `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`.
- `additionalProperties: false` everywhere. Unknown fields are rejected, so changes go through a version bump.
- Vehicle classes are exactly `emergency`, `public`, `private`. P1 maps anything else (e.g. motorbikes) to `private`.
- State labels are exactly `free`, `moderate`, `congested`. Label thresholds are P2's business; triggers use `congestion_score`.

## Rules JSON Schema cannot express (enforced in `serializer.py`)
- `interval_end` > `interval_start`
- `lane_id` unique within a message
- `vehicle_count` == sum of `counts_by_class`
- envelope: `payload_sha256` matches, `origin_node_id` == `payload.node_id`
