#!/usr/bin/env bash
# Container entrypoint. METRICS_SOURCE: synthetic (default) | stdin | file
#   file: replays NDJSON (e.g. saved output of `cv_module --mode detect`) one line per INTERVAL seconds.
set -euo pipefail

case "${METRICS_SOURCE:-synthetic}" in
  stdin)
    exec python -m src.microservice "$@" ;;
  file)
    : "${METRICS_FILE:?METRICS_FILE is required when METRICS_SOURCE=file}"
    exec python -m src.microservice "$@" < <(
      while IFS= read -r line || [ -n "$line" ]; do
        printf '%s\n' "$line"
        sleep "${INTERVAL:-5}"
      done < "$METRICS_FILE"
    ) ;;
  synthetic)
    exec python -m src.microservice "$@" < <(
      exec python -m cv_module --mode synthetic \
        --scenario "${SCENARIO:-free}" --lanes "${LANES:-4}" --interval "${INTERVAL:-5}"
    ) ;;
  *)
    echo "unknown METRICS_SOURCE=${METRICS_SOURCE}" >&2; exit 64 ;;
esac