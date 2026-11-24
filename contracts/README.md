# Contracts

Maintainer: P4 (Monika). Any change needs PR approval from the affected pair.

| File | Direction | Version |
|---|---|---|
| metrics_schema.json | P1 -> P2 | 0.2.0 (draft) |
| state_schema.json | P2 -> P3/P4 | 0.2.0 (draft) |
| envelope_schema.json | P3 <-> P4 | 0.2.0 (draft) |
| bridge_api.md | Python <-> Go | 0.1.0 (draft) |

The three message schemas share one `schema_version` (the constant `SUPPORTED_SCHEMA_VERSION` in `serializer.py`).
`bridge_api.md` is versioned separately.

## Versioning
`MAJOR.MINOR.PATCH`. While MAJOR is 0, a MINOR bump is breaking. Patch = docs/clarifications only. Receivers reject incompatible versions loudly (`UnsupportedVersionError`).

## Conventions
- Timestamps: UTC RFC 3339 with exactly milliseconds, `YYYY-MM-DDTHH:MM:SS.mmmZ`.
- IDs (`node_id`, `lane_id`): `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`.
- `additionalProperties: false` everywhere. Unknown fields are rejected, so changes go through a version bump.
- Vehicle classes are exactly `car`, `motorcycle`, `bus`, `truck`: the COCO classes the pretrained YOLOv8-nano detects. Every class key is required in every lane (use 0 when none).

  | Contract key | COCO id | YOLO name |
  |---|---|---|
  | `car` | 2 | car |
  | `motorcycle` | 3 | motorcycle |
  | `bus` | 5 | bus |
  | `truck` | 7 | truck |

  P1 must filter detections to ids `[2, 3, 5, 7]`. Bicycles (id 1), pedestrians and everything else are ignored. A tracked vehicle is
  counted **once**, under its most frequent class over the track, so a car/truck flicker does not double count.
- State labels are exactly `free`, `moderate`, `congested`. Label thresholds are P2's business; triggers use `congestion_score`.

## Rules JSON Schema cannot express (enforced in `serializer.py`)
- `interval_end` > `interval_start`
- `lane_id` unique within a message
- `vehicle_count` == sum of `counts_by_class`
- envelope: `payload_sha256` matches, `origin_node_id` == `payload.node_id`

## Changelog
- **0.2.0** (breaking): `counts_by_class` keys changed from `emergency`/`public`/`private` to `car`/`motorcycle`/`bus`/`truck`, because YOLOv8-nano
  cannot tell emergency, public and private vehicles apart. Team decision. Nodes still on 0.1.0 reject 0.2.0 messages with `UnsupportedVersionError`, by design.
- **0.1.0**: initial draft.