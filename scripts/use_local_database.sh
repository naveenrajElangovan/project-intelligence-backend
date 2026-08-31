#!/usr/bin/env bash
set -euo pipefail

# Build a local SQLite control plane and switch .env to it, so development does
# not depend on Azure SQL being available.
#
# This does not replace Azure SQL. PI_DATABASE_URL is the only switch: the engine
# already skips the Entra credential chain for any non-mssql URL, and the
# launcher and runner now skip the identity and firewall steps on the same
# condition. Switching back is one line -- the previous value is written into
# .env as PI_DATABASE_URL_AZURE so nothing has to be reconstructed later.
#
# What does not move: Mongo conversations, Chroma vectors, and the Azure Table
# ingestion manifests all live outside this database and are untouched. What does
# not come across: the projects, oauth_states and integration_connections rows,
# because they exist only in the database being left behind. The project record
# is seeded from config/, and Atlassian has to be reconnected once.
#
#   ./scripts/use_local_database.sh
#   ./scripts/use_local_database.sh --project-file config/local-project.DEMO.json
#   ./scripts/use_local_database.sh --revert

SCRIPT_DIRECTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIRECTORY="$(cd "${SCRIPT_DIRECTORY}/.." && pwd)"
ENV_FILE="${PROJECT_DIRECTORY}/.env"
DATABASE_DIRECTORY="${PROJECT_DIRECTORY}/.local-sql"
DATABASE_FILE="${DATABASE_DIRECTORY}/control-plane.db"
PYTHON="${PROJECT_DIRECTORY}/.venv/bin/python"
PROJECT_FILE="${PROJECT_DIRECTORY}/config/local-project.DEMO.json"
REVERT=false

while (($#)); do
  case "$1" in
    --project-file) PROJECT_FILE="$2"; shift 2 ;;
    --revert) REVERT=true; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ ! -x "${PYTHON}" ]]; then
  echo "Create the backend virtual environment and install requirements first." >&2
  exit 1
fi
if [[ ! -f "${ENV_FILE}" ]]; then
  echo "${ENV_FILE} is missing. Copy .env.example and configure it first." >&2
  exit 1
fi

set_env_value() {
  local key="$1" value="$2"
  if grep -q "^${key}=" "${ENV_FILE}"; then
    # A literal replacement, because a connection string contains & and ? which
    # sed would otherwise interpret.
    "${PYTHON}" - "${ENV_FILE}" "${key}" "${value}" <<'REWRITE'
import pathlib, sys

path, key, value = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
lines = path.read_text(encoding="utf-8").splitlines()
path.write_text(
    "\n".join(f"{key}={value}" if line.startswith(f"{key}=") else line for line in lines)
    + "\n",
    encoding="utf-8",
)
REWRITE
  else
    printf '%s=%s\n' "${key}" "${value}" >>"${ENV_FILE}"
  fi
}

read_env_value() {
  local requested_key="$1" line key value
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

if [[ "${REVERT}" == "true" ]]; then
  azure_url="$(read_env_value PI_DATABASE_URL_AZURE)"
  if [[ -z "${azure_url}" ]]; then
    echo "PI_DATABASE_URL_AZURE is not recorded in .env; nothing to revert to." >&2
    exit 1
  fi
  set_env_value PI_DATABASE_URL "${azure_url}"
  echo "Reverted PI_DATABASE_URL to Azure SQL. Restart with prepare_and_start_local_app.sh."
  echo "The SQLite file is left at ${DATABASE_FILE} in case you switch back."
  exit 0
fi

if [[ ! -f "${PROJECT_FILE}" ]]; then
  echo "${PROJECT_FILE} does not exist. Pass --project-file with a project description." >&2
  exit 1
fi

current_url="$(read_env_value PI_DATABASE_URL)"
if [[ "${current_url}" == mssql* ]]; then
  # Recorded once, and never overwritten by a second run, so repeated switching
  # cannot lose the original connection string.
  if [[ -z "$(read_env_value PI_DATABASE_URL_AZURE)" ]]; then
    set_env_value PI_DATABASE_URL_AZURE "${current_url}"
    echo "Recorded the Azure SQL URL as PI_DATABASE_URL_AZURE."
  fi
fi

mkdir -p "${DATABASE_DIRECTORY}"
set_env_value PI_DATABASE_URL "sqlite+aiosqlite:///${DATABASE_FILE}"
echo "PI_DATABASE_URL now points at ${DATABASE_FILE}."

cd "${PROJECT_DIRECTORY}"
# Alembic reads PI_DATABASE_URL through app settings, so it targets the file just
# configured. Migration 0003 is a no-op off Azure SQL: its grants revoke from
# database principals that only exist there.
.venv/bin/alembic upgrade head
.venv/bin/python -m scripts.seed_project_record "${PROJECT_FILE}"

echo
echo "Local control plane ready. Start the stack with ./scripts/prepare_and_start_local_app.sh"
echo "Atlassian must be reconnected once: integration_connections did not come across."
echo "Return to Azure SQL after the free allowance renews with: $0 --revert"
