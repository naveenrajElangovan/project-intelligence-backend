#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIRECTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIRECTORY="$(cd "${SCRIPT_DIRECTORY}/.." && pwd)"

# Compose requires MongoDB initialization credentials separately from the
# authenticated application URL. For local development, derive them from the
# existing git-ignored URL instead of duplicating the password in .env.
if [[ -z "${PI_CHAT_MONGODB_ROOT_USERNAME:-}" || -z "${PI_CHAT_MONGODB_ROOT_PASSWORD:-}" ]]; then
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
' "${PROJECT_DIRECTORY}/.env"
  )"
  IFS=$'\t' read -r PI_CHAT_MONGODB_ROOT_USERNAME PI_CHAT_MONGODB_ROOT_PASSWORD <<< "${mongo_credentials}"
  export PI_CHAT_MONGODB_ROOT_USERNAME PI_CHAT_MONGODB_ROOT_PASSWORD
  unset mongo_credentials
fi

# Use the explicitly configured, already-authenticated SQL identity cache. Read
# only this non-secret path from .env; never source the complete secrets file.
if [[ -z "${PI_DEV_SQL_AZURE_CONFIG_DIR:-}" && -f "${PROJECT_DIRECTORY}/.env" ]]; then
  while IFS='=' read -r key value; do
    if [[ "${key}" == "PI_DEV_SQL_AZURE_CONFIG_DIR" ]]; then
      PI_DEV_SQL_AZURE_CONFIG_DIR="${value}"
      break
    fi
  done < "${PROJECT_DIRECTORY}/.env"
fi

if ! command -v az >/dev/null 2>&1; then
  echo "Azure CLI is required. Install it and run az login first." >&2
  exit 1
fi

run_sql_az() {
  if [[ -n "${PI_DEV_SQL_AZURE_CONFIG_DIR:-}" ]]; then
    AZURE_CONFIG_DIR="${PI_DEV_SQL_AZURE_CONFIG_DIR}" az "$@"
  else
    az "$@"
  fi
}

if ! run_sql_az account show >/dev/null 2>&1; then
  if [[ -n "${PI_DEV_SQL_AZURE_CONFIG_DIR:-}" ]]; then
    echo "The configured unattended Azure SQL credential cache is unavailable or expired." >&2
  else
    echo "No unattended Azure SQL credential cache is configured." >&2
  fi
  exit 1
fi

DEV_SQL_ACCESS_TOKEN="$(
  run_sql_az account get-access-token \
    --resource https://database.windows.net/ \
    --query accessToken \
    --output tsv
)"

if [[ -z "${DEV_SQL_ACCESS_TOKEN}" ]]; then
  echo "Azure CLI returned an empty Azure SQL access token." >&2
  exit 1
fi

cd "${PROJECT_DIRECTORY}"
compose_up_args=(up -d --force-recreate)
if [[ "${PI_DEV_API_REBUILD:-false}" == "true" ]]; then
  compose_up_args+=(--build)
fi
rag_runtime="${PI_DEV_RAG_RUNTIME:-docker}"
if [[ "${rag_runtime}" == "native" ]]; then
  ../project-intelligence-rag/scripts/start_local_macos.sh
  rag_services=(api)
  rag_runtime_url="http://host.docker.internal:8003"
elif [[ "${rag_runtime}" == "docker" ]]; then
  rag_services=(rag api)
  rag_runtime_url="http://rag:8002"
else
  echo "PI_DEV_RAG_RUNTIME must be native or docker." >&2
  exit 1
fi

PI_DATABASE_ACCESS_TOKEN="${DEV_SQL_ACCESS_TOKEN}" \
PI_RAG_RUNTIME_URL="${rag_runtime_url}" \
  docker compose \
    --env-file .env \
    --env-file ../project-intelligence-ingestion/.env \
    "${compose_up_args[@]}" "${rag_services[@]}"

unset DEV_SQL_ACCESS_TOKEN
echo "Backend API started with ${rag_runtime} RAG and an ephemeral Azure SQL token; no token was written to .env."
