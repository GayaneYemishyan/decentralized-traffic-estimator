
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

````markdown
```bash
bash download_samples.sh "DIRECT_VIDEO_URL"
```

### Multiple Lanes

Generate metrics for three lanes:

```bash
python -m cv_module --mode synthetic --lanes 3 --interval 5
```
````

## CV-2: Vehicle Detection and Tracking

The CV module supports real vehicle detection and tracking
using YOLOv8-nano and ByteTrack.

### Features

- Detection of cars, motorcycles, buses, and trucks.
- Vehicle tracking with unique track IDs.
- Configurable counting line and crossing direction.
- Configurable polygon-based region of interest (ROI).
- Duplicate crossing prevention per track ID and line.
- Per-interval vehicle counts and flow rate.
- Average ROI occupancy calculation.
- Vehicle counts grouped by class.

### Running Vehicle Detection

```bash
python -m cv_module --mode detect \
    --video videos/traffic.mp4 \
    --config configs/traffic.json \
    --imgsz 960 \
    --show
```

Remove `--show` to run in headless mode.

### Configuration

The configuration file defines:

- `roi_polygon`: polygon representing the monitored road area.
- `counting_line`: line ID, start point, end point,
  and counting direction.

Coordinates are normalized between 0 and 1, allowing
the configuration to adapt to different frame resolutions.

Supported counting directions:

- `both`
- `positive`
- `negative`

Each camera or video may require its own configuration
depending on the road geometry.

The current sample configuration monitors three road lanes
as one aggregated counting region.

### Benchmark

The benchmark was performed on a laptop using CPU
in headless mode with YOLOv8-nano and image size 960.

| Metric | Result |
|---|---:|
| Processed frames | 401 |
| Video FPS | 19.35 |
| Processing FPS | 24.25 |
| Manual vehicle count | 17 |
| Automatic vehicle count | 17 |
| Relative counting error | 0% |

Processing speed was approximately 25.32% faster
than the source video frame rate.

The counting result is specific to the tested video
and counting line. It does not guarantee the same
accuracy on other videos.

### Testing

```bash
python -m pytest -v
```

Test results: 61 passed.

Tests cover configuration validation, coordinate conversion,
line crossing, duplicate counting prevention, direction
filtering, polygon ROI occupancy, and existing CV functionality.