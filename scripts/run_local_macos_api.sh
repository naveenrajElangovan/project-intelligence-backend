#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIRECTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIRECTORY="$(cd "${SCRIPT_DIRECTORY}/.." && pwd)"

task_azure_config_dir="${PI_DEV_SQL_AZURE_CONFIG_DIR:-}"
if [[ -z "${task_azure_config_dir}" && -f "${PROJECT_DIRECTORY}/.env" ]]; then
  while IFS='=' read -r key value; do
    if [[ "${key}" == "PI_DEV_SQL_AZURE_CONFIG_DIR" ]]; then
      task_azure_config_dir="${value}"
      break
    fi
  done < "${PROJECT_DIRECTORY}/.env"
fi

database_url="${PI_DATABASE_URL:-}"
if [[ -z "${database_url}" && -f "${PROJECT_DIRECTORY}/.env" ]]; then
  while IFS='=' read -r key value; do
    if [[ "${key}" == "PI_DATABASE_URL" ]]; then
      database_url="${value}"
      break
    fi
  done < "${PROJECT_DIRECTORY}/.env"
fi
uses_azure_sql=false
[[ "${database_url}" == mssql* ]] && uses_azure_sql=true

if [[ "${uses_azure_sql}" == "true" && -z "${task_azure_config_dir}" ]]; then
  echo "PI_DEV_SQL_AZURE_CONFIG_DIR is required for renewable Azure SQL authentication." >&2
  exit 1
fi
if [[ ! -x "${PROJECT_DIRECTORY}/.venv/bin/uvicorn" ]]; then
  echo "Create the backend virtual environment and install requirements first." >&2
  exit 1
fi

export PI_DATABASE_ACCESS_TOKEN=
export PI_RAG_SERVICE_URL=http://127.0.0.1:8003

if [[ "${uses_azure_sql}" == "true" ]]; then
  export AZURE_CONFIG_DIR="${task_azure_config_dir}"
  if ! az account show >/dev/null 2>&1; then
    echo "The configured Azure CLI identity is unavailable or needs sign-in." >&2
    exit 1
  fi
fi

cd "${PROJECT_DIRECTORY}"
exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8001
