#!/usr/bin/env sh
# Build the test-only sandbox image from this checkout.
# Usage: ops/sandbox/build.sh [IMAGE_TAG]   (default home-platform-sandbox:local)
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
tag=${1:-home-platform-sandbox:local}
if ! docker buildx version >/dev/null 2>&1; then
  echo "build.sh: the Docker buildx plugin is missing. Install docker-buildx and docker-compose-v2 (Ubuntu), or Docker Engine from Docker's official repository." >&2
  exit 1
fi
DOCKER_BUILDKIT=1 docker build --file "$root/ops/sandbox/Dockerfile" --tag "$tag" "$root"
docker image inspect --format '{{.Id}}' "$tag"
