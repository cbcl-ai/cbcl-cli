#!/usr/bin/env bash
# Build the cbcl-agent Docker image for office containers by hand.
# The daemon builds it itself (container_manager.ensure_image) when it is
# missing or its sources changed; use this helper for a manual or eval build.
#
# The image is labelled with the same cache key the daemon compares
# (image_hash.py), so a daemon started afterwards reuses it instead of
# rebuilding, and the runtime evals accept it as current.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE_TAG="${1:-cbcl-agent:latest}"
# "<label>=<hash>": the label key comes from image_hash.LABEL too.
IMAGE_LABEL="$(python3 -I "${SCRIPT_DIR}/image_hash.py" --label)"

echo "Building ${IMAGE_TAG} (${IMAGE_LABEL}) from ${SCRIPT_DIR}..."
docker build -t "${IMAGE_TAG}" -f "${SCRIPT_DIR}/Dockerfile.agent" \
  --label "${IMAGE_LABEL}" "${SCRIPT_DIR}"
echo "Done: ${IMAGE_TAG}"
