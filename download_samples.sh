#!/usr/bin/env bash
set -euo pipefail

VIDEO_URL="${1:?Usage: bash download_samples.sh VIDEO_URL}"

mkdir -p videos

curl -fL --retry 3 "$VIDEO_URL" -o videos/traffic.mp4

echo "Sample video saved to videos/traffic.mp4"