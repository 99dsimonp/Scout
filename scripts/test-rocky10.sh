#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${SCOUT_ROCKY10_TEST_IMAGE:-scout-rocky10-validation}"
docker build --target validation -f "${ROOT_DIR}/packaging/Dockerfile.rocky10" -t "${IMAGE}" "${ROOT_DIR}"
# No network access is required by tests. HTTP transport tests use container loopback.
docker run --rm --network=none "${IMAGE}"
