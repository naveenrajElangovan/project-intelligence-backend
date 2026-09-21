#!/usr/bin/env bash
set -euo pipefail

# This file is embedded in the encrypted portable bundle. It is not intended to
# be run from a source checkout.
bundle_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
backend_root="$bundle_root/project-intelligence-backend"

command -v docker >/dev/null 2>&1 || {
  echo "Docker Desktop is required." >&2
  exit 1
}
docker compose version >/dev/null 2>&1 || {
  echo "Docker Compose v2 is required." >&2
  exit 1
}
docker info >/dev/null 2>&1 || {
  echo "Start Docker Desktop, then run this installer again." >&2
  exit 1
}

case "$(uname -m)" in
  arm64|aarch64|x86_64|amd64) ;;
  *)
    echo "Unsupported CPU architecture: $(uname -m)" >&2
    exit 1
    ;;
esac

volumes=(
  project-intelligence-backend_project-intelligence-chroma
  project-intelligence-backend_project-intelligence-mongodb-auth
  project-intelligence-backend_provider-secrets
)

for volume in "${volumes[@]}"; do
  if docker volume inspect "$volume" >/dev/null 2>&1; then
    echo "Refusing to overwrite existing Docker volume: $volume" >&2
    echo "Use a clean Docker installation or remove the old Project Intelligence stack first." >&2
    exit 1
  fi
done

restore_volume() {
  local volume="$1"
  local archive="$2"
  docker volume create "$volume" >/dev/null
  docker run --rm \
    -v "$volume:/restore" \
    -v "$bundle_root/data:/backup:ro" \
    alpine:3.21 \
    sh -c "tar -xzf /backup/$archive -C /restore"
}

restore_volume "${volumes[0]}" chroma.tgz
restore_volume "${volumes[1]}" mongodb.tgz
restore_volume "${volumes[2]}" provider-secrets.tgz

cd "$backend_root"
docker compose up -d --build

deadline=$((SECONDS + 300))
while (( SECONDS < deadline )); do
  if docker compose ps --format json | grep -q '"Health":"unhealthy"'; then
    docker compose ps
    echo "A service became unhealthy." >&2
    exit 1
  fi
  healthy="$(docker compose ps --format json | grep -c '"Health":"healthy"' || true)"
  running="$(docker compose ps --services --filter status=running | wc -l | tr -d ' ')"
  if [[ "$healthy" -ge 5 && "$running" -ge 6 ]]; then
    docker compose ps
    echo "Project Intelligence is ready. Backend: http://127.0.0.1:8001"
    exit 0
  fi
  sleep 5
done

docker compose ps
echo "Timed out waiting for Project Intelligence services." >&2
exit 1
