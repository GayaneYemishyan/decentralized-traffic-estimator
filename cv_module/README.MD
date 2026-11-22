
## Computer Vision Module (CV-1)

The CV module provides video replay and synthetic traffic metrics
generation without requiring a machine learning model.

### Synthetic Mode

Generate traffic metrics every second:

```bash
python -m cv_module --mode synthetic --scenario free --interval 1
```

Available scenarios:

- `free` — Free-flowing traffic
- `rising` — Gradually increasing congestion
- `congested` — Sustained congestion
- `jam` — Sudden traffic jam after five intervals

Each output is a JSON object validated against
`contracts/metrics_schema.json`.

Press `Ctrl+C` to stop the generator.

### Video Replay Mode

Replay a local traffic video:

```bash
python -m cv_module --mode replay --video videos/traffic.mp4
```

Loop the video and configure playback FPS:

```bash
python -m cv_module --mode replay --video videos/traffic.mp4 --loop --fps 15
```

Press `q` to stop playback.

### Run Tests

```bash
python -m pytest -v
```

### Metrics Schema Dependency

The synthetic metrics generator follows the shared contract defined in
`contracts/metrics_schema.json` (schema version `0.2.0`).

This schema is maintained by the Orchestration & Ledger module
and is required for JSON validation.

If the schema has not yet been merged into `main`, it must be obtained
from the `orchestration-ledger` branch before running the generator.

From the repository root:

```bash
git fetch origin orchestration-ledger
mkdir -p contracts
git show origin/orchestration-ledger:contracts/metrics_schema.json > contracts/metrics_schema.json
```

Once the schema is merged into `main`, this manual step will no longer
be necessary for new checkouts containing the merged contract.

### Sample Video

Sample videos are not stored in Git.

Download a sample video using:

```bash
bash download_samples.sh "DIRECT_VIDEO_URL"