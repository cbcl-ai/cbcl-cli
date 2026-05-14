#!/usr/bin/env bash
# Build the cbcl-agent Docker image for office containers.
# Called by `cbcl start` if the image doesn't exist, or manually.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE_TAG="${1:-cbcl-agent:latest}"

echo "Building ${IMAGE_TAG} from ${SCRIPT_DIR}..."
docker build -t "${IMAGE_TAG}" -f "${SCRIPT_DIR}/Dockerfile.agent" "${SCRIPT_DIR}"
echo "Done: ${IMAGE_TAG}"
