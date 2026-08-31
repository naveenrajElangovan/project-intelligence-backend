#!/usr/bin/env bash
# Build every service image for the server's architecture and ship only the
# images -- never the source.
#
#   ./scripts/release_images.sh push 2026-08-28.1                 # to a registry
#   ./scripts/release_images.sh save 2026-08-28.1 /tmp/release     # to tarballs
#
# Run from the backend repo. The three repos must be siblings on disk.
set -euo pipefail

MODE="${1:?usage: release_images.sh <push|save> <tag> [out-dir]}"
TAG="${2:?give an immutable tag, e.g. 2026-08-28.1}"
OUT_DIR="${3:-./release}"
REGISTRY="${PI_IMAGE_REGISTRY:-}"
# Apple Silicon builds arm64 by default; almost every cheap VPS is amd64. An
# image built for the wrong architecture starts and then dies with "exec format
# error", so the platform is pinned rather than inherited.
PLATFORM="${PI_IMAGE_PLATFORM:-linux/amd64}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SIBLINGS="$(dirname "$HERE")"

build_one() {
  local name="$1" context="$2" dockerfile="${3:-Dockerfile}"
  local reference="$name:$TAG"
  [[ -n "$REGISTRY" ]] && reference="$REGISTRY/$reference"
  echo "==> $reference  ($PLATFORM)"
  docker build \
    --platform "$PLATFORM" \
    --file "$context/$dockerfile" \
    --tag "$reference" \
    "$context"
  echo "$reference"
}

REFERENCES=()
REFERENCES+=("$(build_one project-intelligence-backend "$HERE" | tail -1)")
REFERENCES+=("$(build_one project-intelligence-rag "$SIBLINGS/project-intelligence-rag" | tail -1)")
REFERENCES+=("$(build_one project-intelligence-ingestion "$SIBLINGS/project-intelligence-ingestion" | tail -1)")
REFERENCES+=("$(build_one project-intelligence-ingestion-worker "$SIBLINGS/project-intelligence-ingestion" Dockerfile.worker | tail -1)")

# The models image is versioned by model revision, not by application version, so
# it is built only when PI_MODELS_TAG is set -- otherwise the 2.5 GB of weights
# would be rebuilt and re-pushed on every application release.
if [[ -n "${PI_MODELS_TAG:-}" ]]; then
  models_reference="project-intelligence-models:$PI_MODELS_TAG"
  [[ -n "$REGISTRY" ]] && models_reference="$REGISTRY/$models_reference"
  echo "==> $models_reference  ($PLATFORM)"
  docker build \
    --platform "$PLATFORM" \
    --file "$SIBLINGS/project-intelligence-rag/Dockerfile.models" \
    --tag "$models_reference" \
    "$SIBLINGS/project-intelligence-rag"
  REFERENCES+=("$models_reference")
fi

case "$MODE" in
  push)
    [[ -n "$REGISTRY" ]] || { echo "PI_IMAGE_REGISTRY must be set to push" >&2; exit 2; }
    for reference in "${REFERENCES[@]}"; do
      echo "==> push $reference"
      docker push "$reference"
    done
    echo
    echo "On the server:"
    echo "  export PI_IMAGE_REGISTRY=$REGISTRY PI_IMAGE_TAG=$TAG"
    echo "  docker compose -f docker-compose.release.yml pull"
    ;;
  save)
    mkdir -p "$OUT_DIR"
    archive="$OUT_DIR/project-intelligence-$TAG.tar"
    echo "==> saving ${#REFERENCES[@]} images to $archive"
    docker save -o "$archive" "${REFERENCES[@]}"
    gzip -f "$archive"
    echo
    echo "Copy $archive.gz to the server, then:"
    echo "  gunzip -c project-intelligence-$TAG.tar.gz | docker load"
    ;;
  *)
    echo "mode must be push or save" >&2
    exit 2
    ;;
esac

echo
echo "Digests (pin these):"
for reference in "${REFERENCES[@]}"; do
  docker image inspect "$reference" --format '{{index .RepoDigests 0}}' 2>/dev/null \
    || docker image inspect "$reference" --format '{{.Id}}  '"$reference"
done
