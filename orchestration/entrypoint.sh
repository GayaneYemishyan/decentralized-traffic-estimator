#!/usr/bin/env bash
# Container entrypoint: P1 (synthetic generator) -> P2+P4 (microservice) inside one node.
# exec makes the microservice PID 1's child under tini, so SIGTERM reaches it directly and
# its exit code (0 / 2 / 3) becomes the container's exit code.
# METRICS_SOURCE=stdin skips the generator (feed real CV output with `docker attach` or a pipe).
set -euo pipefail

if [ "${METRICS_SOURCE:-synthetic}" = "stdin" ]; then
  exec python -m src.microservice "$@"
fi

exec python -m src.microservice "$@" < <(
  exec python -m cv_module --mode synthetic \
    --scenario "${SCENARIO:-free}" --lanes "${LANES:-4}" --interval "${INTERVAL:-5}"
)