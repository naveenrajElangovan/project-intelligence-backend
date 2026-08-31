#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIRECTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIRECTORY="$(cd "${SCRIPT_DIRECTORY}/.." && pwd)"
RAG_DIRECTORY="$(cd "${PROJECT_DIRECTORY}/../project-intelligence-rag" && pwd)"
ENV_FILE="${PROJECT_DIRECTORY}/.env"
RUNTIME_DIRECTORY="${PROJECT_DIRECTORY}/.run"
API_PID_FILE="${RUNTIME_DIRECTORY}/backend-api.pid"
API_LOG_FILE="${RUNTIME_DIRECTORY}/backend-api.log"

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

explain_readiness_failure() {
  # "Review the log" costs a round trip through 500 KB of stack traces for a
  # cause the log already states in one line. Surface the known ones directly.
  local log="$1"

  [[ -f "${log}" ]] || return 0
  if grep -q "monthly free amount allowance\|42119" "${log}"; then
    echo "Cause: Azure SQL paused this database after reaching the free-tier" >&2
    echo "monthly allowance. No script here can restore it. Wait for the renewal" >&2
    echo "at 00:00 UTC on the first of next month, or open the database's Compute" >&2
    echo "and Storage tab in the Azure Portal and choose 'Continue using database" >&2
    echo "with additional charges'." >&2
  elif grep -q "is not allowed to access the server\|40615" "${log}"; then
    echo "Cause: this host's public IP is not in the SQL firewall rule." >&2
    echo "Run ./scripts/sync_sql_access.sh and start again." >&2
  elif grep -q "Login failed\|18456" "${log}"; then
    echo "Cause: Azure SQL rejected the identity. Check role membership with" >&2
    echo "./scripts/check_runtime_identity.py." >&2
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

require_command az
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

mongodb_container_id="$(docker ps -aq --filter label=com.docker.compose.service=mongodb | head -n 1)"
if [[ -n "${mongodb_container_id}" ]]; then
  if [[ "$(docker inspect --format '{{.State.Running}}' "${mongodb_container_id}")" != "true" ]]; then
    docker start "${mongodb_container_id}" >/dev/null
  fi
else
  mongo_username="$(read_env_value PI_CHAT_MONGODB_ROOT_USERNAME)"
  mongo_password="$(read_env_value PI_CHAT_MONGODB_ROOT_PASSWORD)"
  mongo_docker_url="$(read_env_value PI_CHAT_MONGODB_DOCKER_URL)"
  if [[ -z "${mongo_username}" || -z "${mongo_password}" || -z "${mongo_docker_url}" ]]; then
    echo "MongoDB has not been initialized. Set PI_CHAT_MONGODB_ROOT_USERNAME," >&2
    echo "PI_CHAT_MONGODB_ROOT_PASSWORD, and PI_CHAT_MONGODB_DOCKER_URL in .env." >&2
    exit 1
  fi
  (
    cd "${PROJECT_DIRECTORY}"
    docker compose --env-file .env up -d mongodb
  )
  mongodb_container_id="$(docker ps -aq --filter label=com.docker.compose.service=mongodb | head -n 1)"
fi
if [[ -z "${mongodb_container_id}" ]]; then
  echo "MongoDB was not created successfully." >&2
  exit 1
fi
wait_for_mongodb "${mongodb_container_id}"
echo "MongoDB is healthy."

docker_api_container_id="$(docker ps -aq --filter label=com.docker.compose.service=api | head -n 1)"
if [[ -n "${docker_api_container_id}" ]]; then
  if [[ ! -s "${local_secret_store_path}" ]]; then
    temporary_secret_store="$(mktemp "${local_secret_store_path}.restore.XXXXXX")"
    if docker cp \
      "${docker_api_container_id}:/var/lib/project-intelligence/provider-secrets.json" \
      "${temporary_secret_store}" >/dev/null 2>&1; then
      chmod 0600 "${temporary_secret_store}"
      mv "${temporary_secret_store}" "${local_secret_store_path}"
      echo "Restored the encrypted provider credential store for the native backend."
    else
      rm -f "${temporary_secret_store}"
    fi
  fi
  if [[ "$(docker inspect --format '{{.State.Running}}' "${docker_api_container_id}")" == "true" ]]; then
    docker stop "${docker_api_container_id}" >/dev/null
    echo "Stopped the fixed-token Docker backend before starting the renewable local backend."
  fi
fi

"${RAG_DIRECTORY}/scripts/start_local_macos.sh"

if curl --fail --silent http://127.0.0.1:8001/health >/dev/null 2>&1; then
  # Health does not say which revision is answering. Because this launcher is
  # idempotent, an edit would otherwise look like it had no effect: the process
  # keeps serving the code it started with. Refuse rather than mislead.
  if [[ ! -f "${API_PID_FILE}" ]]; then
    echo "Something is serving http://127.0.0.1:8001 but ${API_PID_FILE} is absent," >&2
    echo "so this launcher did not start it and cannot tell which code it runs." >&2
    echo "Stop it and start again:" >&2
    echo "  ./scripts/stop_local_macos_api.sh && ./scripts/prepare_and_start_local_app.sh" >&2
    exit 1
  fi
  stale=""
  if [[ -f "${API_PID_FILE}" ]]; then
    # Only what the running process loaded. scripts/ affects the next launch and
    # requirements.txt does not change the installed venv, so including them made
    # the check fire on edits that cannot possibly affect the live service.
    stale="$(cd "${PROJECT_DIRECTORY}" && find app .env \
      -newer "${API_PID_FILE}" -print -quit 2>/dev/null || true)"
  fi
  if [[ -n "${stale}" && "${PI_DEV_ALLOW_STALE_API:-false}" != "true" ]]; then
    # Self-heal rather than refuse. This launcher is idempotent and already knows
    # the fix, so printing it and exiting 1 just interrupted whatever was calling
    # it -- including run_unattended_ingestion.sh. Stop the stale process and
    # re-execute, with a guard so a condition that survives a restart fails once
    # instead of looping.
    if [[ "${PI_DEV_API_RESTARTED:-false}" == "true" ]]; then
      echo "The backend still reports stale code (${stale}) after a restart." >&2
      echo "Something is rewriting app/ or .env while it starts." >&2
      exit 1
    fi
    echo "Backend is running older code than disk (changed: ${stale}). Restarting it."
    "${SCRIPT_DIRECTORY}/stop_local_macos_api.sh" || true
    PI_DEV_API_RESTARTED=true exec "${BASH_SOURCE[0]}" "$@"
  fi
  echo "Backend is already healthy at http://127.0.0.1:8001."
else
  mkdir -p "${RUNTIME_DIRECTORY}"
  cd "${PROJECT_DIRECTORY}"
  nohup ./scripts/run_local_macos_api.sh >"${API_LOG_FILE}" 2>&1 &
  api_pid="$!"
  echo "${api_pid}" >"${API_PID_FILE}"

  for _ in {1..45}; do
    if curl --fail --silent http://127.0.0.1:8001/health >/dev/null 2>&1; then
      break
    fi
    if ! kill -0 "${api_pid}" 2>/dev/null; then
      echo "Backend startup failed. Review ${API_LOG_FILE}." >&2
  explain_readiness_failure "${API_LOG_FILE}"
      exit 1
    fi
    sleep 1
  done
fi

ready=false
for _ in {1..60}; do
  if curl --fail --silent --max-time 10 http://127.0.0.1:8001/ready >/dev/null; then
    ready=true
    break
  fi
  sleep 2
done
if [[ "${ready}" != "true" ]]; then
  echo "Backend started, but a dependency is not ready. Review ${API_LOG_FILE}." >&2
  explain_readiness_failure "${API_LOG_FILE}"
  exit 1
fi

echo "Local services are ready. You can now start the application."
echo "Backend: http://127.0.0.1:8001"
echo "RAG:     http://127.0.0.1:8003"
