#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIRECTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIRECTORY="$(cd "${SCRIPT_DIRECTORY}/.." && pwd)"
RAG_DIRECTORY="$(cd "${PROJECT_DIRECTORY}/../project-intelligence-rag" && pwd)"
ENV_FILE="${PROJECT_DIRECTORY}/.env"

read_env_value() {
  local requested_key="$1"
  local line key value

  [[ -f "${ENV_FILE}" ]] || return 0
  while IFS= read -r line || [[ -n "${line}" ]]; do
    key="${line%%=*}"
    if [[ "${key}" == "${requested_key}" ]]; then
      value="${line#*=}"
      printf '%s' "${value%$'\r'}"
      return 0
    fi
  done < "${ENV_FILE}"
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "$1 is required before starting the local application services." >&2
    exit 1
  fi
}

wait_for_mongodb() {
  local container_id="$1"
  local state

  for _ in {1..30}; do
    state="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "${container_id}")"
    if [[ "${state}" == "healthy" || "${state}" == "running" ]]; then
      return 0
    fi
    if [[ "${state}" == "exited" || "${state}" == "dead" ]]; then
      break
    fi
    sleep 1
  done
  echo "MongoDB did not become healthy. Check container logs before starting the application." >&2
  exit 1
}

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This launcher is intended for local macOS development." >&2
  exit 1
fi
if [[ ! -f "${ENV_FILE}" ]]; then
  echo "${ENV_FILE} is missing. Copy .env.example and configure it first." >&2
  exit 1
fi

require_command curl
require_command docker

local_secret_store_path="${PI_LOCAL_SECRET_STORE_PATH:-$(read_env_value PI_LOCAL_SECRET_STORE_PATH)}"
local_secret_store_path="${local_secret_store_path:-.local-secrets.json}"
if [[ "${local_secret_store_path}" != /* ]]; then
  local_secret_store_path="${PROJECT_DIRECTORY}/${local_secret_store_path}"
fi

# Only when the control plane is Azure SQL. A local SQLite control plane needs no
# Entra token and no firewall rule, and requiring an Azure CLI sign-in to start a
# fully local stack would defeat the point of having one.
database_url="${PI_DATABASE_URL:-$(read_env_value PI_DATABASE_URL)}"
if [[ "${database_url}" == mssql* ]]; then
  require_command az
  # One implementation of the identity and firewall checks, shared with the
  # standalone script so a mid-session IP change is repairable without a restart.
  "${SCRIPT_DIRECTORY}/sync_sql_access.sh"
else
  echo "Control plane is local (${database_url%%:*}); skipping Azure SQL identity and firewall."
fi

if ! docker info >/dev/null 2>&1; then
  echo "Docker Desktop is not running." >&2
  exit 1
fi

# Compose needs MongoDB initialization credentials even when only Chroma is
# selected because interpolation validates the complete file. Derive them from
# the existing authenticated, git-ignored URL instead of storing a second copy.
mongo_username="${PI_CHAT_MONGODB_ROOT_USERNAME:-$(read_env_value PI_CHAT_MONGODB_ROOT_USERNAME)}"
mongo_password="${PI_CHAT_MONGODB_ROOT_PASSWORD:-$(read_env_value PI_CHAT_MONGODB_ROOT_PASSWORD)}"
mongo_docker_url="$(read_env_value PI_CHAT_MONGODB_DOCKER_URL)"
if [[ -z "${mongo_username}" || -z "${mongo_password}" ]]; then
  mongo_credentials="$(
    python3 -c '
from pathlib import Path
from urllib.parse import unquote, urlsplit
import sys

value = ""
for line in Path(sys.argv[1]).read_text().splitlines():
    if line.startswith("PI_CHAT_MONGODB_DOCKER_URL="):
        value = line.split("=", 1)[1].strip()
        break
parsed = urlsplit(value)
if not parsed.username or parsed.password is None:
    raise SystemExit("PI_CHAT_MONGODB_DOCKER_URL must contain URL-encoded credentials.")
print(f"{unquote(parsed.username)}\t{unquote(parsed.password)}", end="")
' "${ENV_FILE}"
  )"
  IFS=$'\t' read -r mongo_username mongo_password <<< "${mongo_credentials}"
  unset mongo_credentials
fi
export PI_CHAT_MONGODB_ROOT_USERNAME="${mongo_username}"
export PI_CHAT_MONGODB_ROOT_PASSWORD="${mongo_password}"

# These are this launcher's data dependencies. Starting them here prevents RAG
# from entering its lexical warm-up while Chroma is absent.
(
  cd "${PROJECT_DIRECTORY}"
  docker compose --env-file .env up -d chroma mongodb
)
mongodb_container_id="$(
  cd "${PROJECT_DIRECTORY}"
  docker compose --env-file .env ps -q mongodb
)"
if [[ -z "${mongodb_container_id}" ]]; then
  echo "MongoDB was not created successfully." >&2
  exit 1
fi
wait_for_mongodb "${mongodb_container_id}"
echo "MongoDB is healthy."

# A fresh official Mongo image creates only the root user in `admin`. The
# application URL intentionally authenticates against its own database, so a
# new volume otherwise passes the unauthenticated healthcheck and then makes the
# backend die with AuthenticationFailed. Create the scoped application user
# idempotently, using credentials already present inside the container; never
# print or duplicate the password.
chat_database="$(read_env_value PI_CHAT_MONGODB_DATABASE)"
if [[ -z "${chat_database}" ]]; then
  echo "PI_CHAT_MONGODB_DATABASE must be set before initializing chat storage." >&2
  exit 1
fi
docker exec \
  --env "PI_CHAT_APP_DATABASE=${chat_database}" \
  "${mongodb_container_id}" \
  sh -c 'mongosh --quiet \
    --username "$MONGO_INITDB_ROOT_USERNAME" \
    --password "$MONGO_INITDB_ROOT_PASSWORD" \
    --authenticationDatabase admin \
    --eval '\''
      const name = process.env.PI_CHAT_APP_DATABASE;
      const target = db.getSiblingDB(name);
      if (target.getUser(process.env.MONGO_INITDB_ROOT_USERNAME) === null) {
        target.createUser({
          user: process.env.MONGO_INITDB_ROOT_USERNAME,
          pwd: process.env.MONGO_INITDB_ROOT_PASSWORD,
          roles: [{role: "readWrite", db: name}]
        });
      }
    '\''' >/dev/null
echo "MongoDB application user is ready."

if ! curl --fail --silent --max-time 5 http://127.0.0.1:8000/api/v2/heartbeat >/dev/null; then
  echo "Chroma did not become reachable on http://127.0.0.1:8000." >&2
  exit 1
fi
echo "Chroma is healthy."

"${RAG_DIRECTORY}/scripts/stop_local_macos.sh" >/dev/null 2>&1 || true
"${SCRIPT_DIRECTORY}/stop_local_macos_api.sh" >/dev/null 2>&1 || true

# Keep the default stack self-contained. A detached macOS accelerator can be
# terminated by the OS or a terminal session, which previously made every
# project fail at embedding or reranking while the containers still looked up.
# The container already mounts the same pinned models and safely uses CPU.
use_host_accelerator="${PI_RAG_USE_HOST_ACCELERATOR:-$(read_env_value PI_RAG_USE_HOST_ACCELERATOR)}"
use_host_accelerator="${use_host_accelerator:-false}"
accelerator_key_file="${RAG_DIRECTORY}/.run/accelerator.key"
if [[ "${use_host_accelerator}" == "true" ]]; then
  "${RAG_DIRECTORY}/scripts/start_accelerator_macos.sh"
else
  mkdir -p "${RAG_DIRECTORY}/.run"
  chmod 0700 "${RAG_DIRECTORY}/.run"
  if [[ ! -s "${accelerator_key_file}" ]]; then
    umask 077
    openssl rand -hex 32 >"${accelerator_key_file}"
  fi
  echo "Using self-contained Docker embedding and reranking models."
fi
if [[ -z "${PI_RAG_INTERNAL_API_KEY:-}" ]]; then
  PI_RAG_INTERNAL_API_KEY="$(<"${accelerator_key_file}")"
  export PI_RAG_INTERNAL_API_KEY
fi

echo "Building and starting Docker RAG and backend API."
(
  cd "${PROJECT_DIRECTORY}"
  docker compose --env-file .env up -d --build rag api
)

api_container_id="$(
  cd "${PROJECT_DIRECTORY}"
  docker compose --env-file .env ps -q api
)"
if [[ -s "${local_secret_store_path}" ]] \
  && ! docker exec "${api_container_id}" \
    sh -c 'test -s /var/lib/project-intelligence/provider-secrets.json && test -r /var/lib/project-intelligence/provider-secrets.json'; then
  (
    cd "${PROJECT_DIRECTORY}"
    docker compose --env-file .env run --rm --no-deps \
      --volume "${local_secret_store_path}:/source/provider-secrets.json:ro" \
      --entrypoint /bin/sh secrets-init -c \
      'cp /source/provider-secrets.json /var/lib/project-intelligence/provider-secrets.json \
        && chown app:app /var/lib/project-intelligence/provider-secrets.json \
        && chmod 0600 /var/lib/project-intelligence/provider-secrets.json'
  )
  docker restart "${api_container_id}" >/dev/null
  echo "Migrated the encrypted provider credential store into Docker."
fi

rag_ready=false
backend_ready=false
for _ in {1..120}; do
  if curl --fail --silent --max-time 5 http://127.0.0.1:8003/ready >/dev/null 2>&1; then
    rag_ready=true
  fi
  if curl --fail --silent --max-time 5 http://127.0.0.1:8001/ready >/dev/null 2>&1; then
    backend_ready=true
  fi
  if [[ "${rag_ready}" == "true" && "${backend_ready}" == "true" ]]; then
    break
  fi
  sleep 2
done
if [[ "${rag_ready}" != "true" || "${backend_ready}" != "true" ]]; then
  echo "The Docker application stack did not become ready." >&2
  (
    cd "${PROJECT_DIRECTORY}"
    docker compose --env-file .env ps
    docker compose --env-file .env logs --tail=120 rag api
  ) >&2
  exit 1
fi

echo "Docker services are ready. You can now start the application."
echo "Backend: http://127.0.0.1:8001"
echo "RAG:     http://127.0.0.1:8003"
