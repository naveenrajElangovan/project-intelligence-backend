#!/usr/bin/env bash
set -euo pipefail

# Keep local Azure SQL access working: verify the CLI identity can still mint a
# token, and point the developer firewall rule at the current public IP.
#
# Why this is its own script. Both checks used to live inside
# prepare_and_start_local_app.sh and therefore ran exactly once, at startup. But
# the engine sets pool_recycle to PI_DATABASE_POOL_RECYCLE_SECONDS, so every
# pooled connection is rebuilt on that cadence. If the laptop's public IP changes
# after startup -- different Wi-Fi, a VPN, an ISP lease renewal -- the rule still
# names the old address and each rebuild is refused by the server. The symptom is
# a session that works and then "keeps disconnecting" on a fixed interval, with
# nothing wrong in the application at all.
#
# In-process token refresh is not the problem: AzureSqlAccessTokenProvider
# renews through DefaultAzureCredential with a margin, and only the Docker path
# (PI_DATABASE_ACCESS_TOKEN, StaticAccessTokenProvider) holds a token it cannot
# renew. What expires without anyone noticing is the CLI sign-in behind it, which
# this script reports plainly.
#
#   ./scripts/sync_sql_access.sh            # check and fix once
#   ./scripts/sync_sql_access.sh --watch    # keep it correct while you work
#   ./scripts/sync_sql_access.sh --watch 300

SCRIPT_DIRECTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIRECTORY="$(cd "${SCRIPT_DIRECTORY}/.." && pwd)"
ENV_FILE="${PROJECT_DIRECTORY}/.env"

WATCH=false
INTERVAL=120
while (($#)); do
  case "$1" in
    --watch)
      WATCH=true
      shift
      if [[ "${1:-}" =~ ^[0-9]+$ ]]; then
        INTERVAL="$1"
        shift
      fi
      ;;
    --interval)
      INTERVAL="${2:?--interval needs seconds}"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done
if ((INTERVAL < 30)); then
  echo "An interval below 30 seconds only adds Azure CLI calls; nothing changes that fast." >&2
  exit 2
fi

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

is_valid_ipv4() {
  local address="$1"
  local octet
  local -a octets

  [[ "${address}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
  IFS='.' read -r -a octets <<< "${address}"
  [[ "${#octets[@]}" -eq 4 ]] || return 1
  for octet in "${octets[@]}"; do
    [[ "${octet}" =~ ^[0-9]{1,3}$ ]] || return 1
    ((10#${octet} <= 255)) || return 1
  done
}

for command_name in az curl; do
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    echo "${command_name} is required to manage local Azure SQL access." >&2
    exit 1
  fi
done
if [[ ! -f "${ENV_FILE}" ]]; then
  echo "${ENV_FILE} is missing. Copy .env.example and configure it first." >&2
  exit 1
fi

azure_config_dir="${PI_DEV_SQL_AZURE_CONFIG_DIR:-$(read_env_value PI_DEV_SQL_AZURE_CONFIG_DIR)}"
database_url="${PI_DATABASE_URL:-$(read_env_value PI_DATABASE_URL)}"
sql_resource_group="${PI_DEV_SQL_RESOURCE_GROUP:-$(read_env_value PI_DEV_SQL_RESOURCE_GROUP)}"
sql_server="${PI_DEV_SQL_SERVER:-$(read_env_value PI_DEV_SQL_SERVER)}"
firewall_rule_name="${PI_DEV_SQL_FIREWALL_RULE_NAME:-$(read_env_value PI_DEV_SQL_FIREWALL_RULE_NAME)}"
firewall_sync="${PI_DEV_SQL_FIREWALL_SYNC:-$(read_env_value PI_DEV_SQL_FIREWALL_SYNC)}"

sql_resource_group="${sql_resource_group:-examplecorp}"
firewall_rule_name="${firewall_rule_name:-LocalDeveloperCurrentIP}"
firewall_sync="${firewall_sync:-true}"

if [[ -z "${sql_server}" && "${database_url}" == mssql+aioodbc://@*.database.windows.net/* ]]; then
  database_host="${database_url#mssql+aioodbc://@}"
  database_host="${database_host%%/*}"
  sql_server="${database_host%.database.windows.net}"
fi
if [[ -z "${azure_config_dir}" || ! -d "${azure_config_dir}" ]]; then
  echo "PI_DEV_SQL_AZURE_CONFIG_DIR must point to an existing Azure CLI login cache." >&2
  exit 1
fi
if [[ -z "${sql_server}" ]]; then
  echo "PI_DEV_SQL_SERVER is required because the SQL server could not be derived from PI_DATABASE_URL." >&2
  exit 1
fi
if [[ "${firewall_sync}" != "true" && "${firewall_sync}" != "false" ]]; then
  echo "PI_DEV_SQL_FIREWALL_SYNC must be true or false." >&2
  exit 1
fi

sql_az() {
  AZURE_CONFIG_DIR="${azure_config_dir}" az "$@"
}

check_identity() {
  if ! sql_az account show >/dev/null 2>&1; then
    echo "The configured Azure CLI identity needs sign-in:" >&2
    echo "  AZURE_CONFIG_DIR=${azure_config_dir} az login" >&2
    return 1
  fi
  # account show reads the cache; only requesting a token proves the refresh
  # token behind it is still valid, which is the thing that silently expires.
  if ! sql_az account get-access-token \
    --resource https://database.windows.net/ \
    --query accessToken \
    --output tsv >/dev/null 2>&1; then
    echo "The Azure CLI sign-in has expired and can no longer mint a SQL token:" >&2
    echo "  AZURE_CONFIG_DIR=${azure_config_dir} az login" >&2
    return 1
  fi
  return 0
}

sync_firewall() {
  local public_ip allowed_ip

  [[ "${firewall_sync}" == "true" ]] || return 0
  if ! public_ip="$(curl --ipv4 --fail --silent --show-error --max-time 10 https://api.ipify.org)"; then
    echo "The public IPv4 lookup failed; the SQL firewall was left unchanged." >&2
    return 1
  fi
  if ! is_valid_ipv4 "${public_ip}"; then
    echo "The public IPv4 lookup returned an invalid value; the SQL firewall was left unchanged." >&2
    return 1
  fi
  allowed_ip="$(sql_az sql server firewall-rule show \
    --resource-group "${sql_resource_group}" \
    --server "${sql_server}" \
    --name "${firewall_rule_name}" \
    --query startIpAddress \
    --output tsv 2>/dev/null || true)"
  if [[ "${allowed_ip}" == "${public_ip}" ]]; then
    return 0
  fi
  sql_az sql server firewall-rule create \
    --resource-group "${sql_resource_group}" \
    --server "${sql_server}" \
    --name "${firewall_rule_name}" \
    --start-ip-address "${public_ip}" \
    --end-ip-address "${public_ip}" \
    --output none
  echo "Azure SQL firewall rule ${firewall_rule_name} moved from ${allowed_ip:-none} to ${public_ip}."
  return 0
}

if [[ "${WATCH}" != "true" ]]; then
  check_identity
  echo "Azure SQL identity is available."
  sync_firewall || exit 1
  echo "Azure SQL access is current."
  exit 0
fi

echo "Watching Azure SQL access every ${INTERVAL}s. Ctrl-C to stop."
while true; do
  # A transient failure must not end the watch: the next pass is the retry. Only
  # a lost sign-in is worth shouting about, because nothing here can fix it.
  if check_identity; then
    sync_firewall || true
  fi
  sleep "${INTERVAL}"
done
