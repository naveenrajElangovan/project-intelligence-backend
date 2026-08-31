#!/usr/bin/env bash
# Clear the local conversation store (MongoDB `conversations` and `turns`).
#
# Dry run by default: it reports what WOULD be deleted and changes nothing.
# Deleting requires --apply and a typed confirmation.
#
#   ./scripts/clear_local_chat_store.sh                      # report only
#   ./scripts/clear_local_chat_store.sh --project DEMO       # report one project
#   ./scripts/clear_local_chat_store.sh --project DEMO --apply
#   ./scripts/clear_local_chat_store.sh --apply              # every project
#
# Documents are removed; the collections, their indexes and the TTL policy are
# left in place, so the application does not need to re-create them.
#
# Note: conversation documents carry the semantic context that resolves
# follow-up questions ("these are the only fields?"). Clearing them resets each
# thread's active subject until it is rebuilt by new turns.

set -euo pipefail

SCRIPT_DIRECTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(cd "${SCRIPT_DIRECTORY}/.." && pwd)"
ENV_FILE="${REPOSITORY_ROOT}/.env"

APPLY=false
PROJECT_ID=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=true; shift ;;
    --project) PROJECT_ID="${2-}"; shift 2 ;;
    --project=*) PROJECT_ID="${1#*=}"; shift ;;
    -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

# Same reader the launcher uses, so both scripts agree on one source of truth.
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

MONGODB_URL="${PI_CHAT_MONGODB_URL:-$(read_env_value PI_CHAT_MONGODB_URL)}"
MONGODB_DATABASE="${PI_CHAT_MONGODB_DATABASE:-$(read_env_value PI_CHAT_MONGODB_DATABASE)}"
MONGODB_DATABASE="${MONGODB_DATABASE:-project_intelligence_chat}"

if [[ -z "${MONGODB_URL}" ]]; then
  echo "PI_CHAT_MONGODB_URL is not set in the environment or ${ENV_FILE}." >&2
  exit 1
fi

PYTHON_BINARY="${REPOSITORY_ROOT}/.venv/bin/python"
[[ -x "${PYTHON_BINARY}" ]] || PYTHON_BINARY="$(command -v python3)"
if [[ ! -x "${PYTHON_BINARY}" ]]; then
  echo "No Python interpreter found. Create ${REPOSITORY_ROOT}/.venv first." >&2
  exit 1
fi

# Counting first means the confirmation prompt states real numbers rather than
# asking for a blind yes.
COUNTS="$(
  PI_CLEAR_URL="${MONGODB_URL}" \
  PI_CLEAR_DB="${MONGODB_DATABASE}" \
  PI_CLEAR_PROJECT="${PROJECT_ID}" \
  "${PYTHON_BINARY}" - <<'PYTHON'
import asyncio, os, sys

try:
    from pymongo import AsyncMongoClient
except ImportError:
    sys.exit("pymongo is not installed in this interpreter.")


async def main() -> None:
    project = os.environ.get("PI_CLEAR_PROJECT") or ""
    client = AsyncMongoClient(
        os.environ["PI_CLEAR_URL"], serverSelectionTimeoutMS=3000, connectTimeoutMS=3000
    )
    try:
        await client.admin.command("ping")
        database = client[os.environ["PI_CLEAR_DB"]]
        conversation_filter = {"project_id": project} if project else {}
        conversations = await database["conversations"].count_documents(conversation_filter)
        if project:
            ids = [
                document["_id"]
                async for document in database["conversations"].find(
                    conversation_filter, {"_id": 1}
                )
            ]
            turns = await database["turns"].count_documents(
                {"conversation_id": {"$in": ids}}
            )
        else:
            turns = await database["turns"].count_documents({})
        print(f"{conversations} {turns}")
    finally:
        await client.close()


asyncio.run(main())
PYTHON
)"

read -r CONVERSATION_COUNT TURN_COUNT <<<"${COUNTS}"
SCOPE_LABEL="every project"
[[ -n "${PROJECT_ID}" ]] && SCOPE_LABEL="project ${PROJECT_ID}"

echo "Database : ${MONGODB_DATABASE}"
echo "Scope    : ${SCOPE_LABEL}"
echo "Would remove: ${CONVERSATION_COUNT} conversation(s), ${TURN_COUNT} turn(s)"

if [[ "${APPLY}" != true ]]; then
  echo
  echo "Dry run. Nothing was deleted. Re-run with --apply to delete."
  exit 0
fi

if [[ "${CONVERSATION_COUNT}" == "0" && "${TURN_COUNT}" == "0" ]]; then
  echo "Nothing to delete."
  exit 0
fi

echo
echo "This permanently deletes conversation history for ${SCOPE_LABEL}."
printf 'Type DELETE to continue: '
read -r CONFIRMATION
if [[ "${CONFIRMATION}" != "DELETE" ]]; then
  echo "Aborted. Nothing was deleted."
  exit 1
fi

PI_CLEAR_URL="${MONGODB_URL}" \
PI_CLEAR_DB="${MONGODB_DATABASE}" \
PI_CLEAR_PROJECT="${PROJECT_ID}" \
"${PYTHON_BINARY}" - <<'PYTHON'
import asyncio, os, sys

try:
    from pymongo import AsyncMongoClient
except ImportError:
    sys.exit("pymongo is not installed in this interpreter.")


async def main() -> None:
    project = os.environ.get("PI_CLEAR_PROJECT") or ""
    client = AsyncMongoClient(
        os.environ["PI_CLEAR_URL"], serverSelectionTimeoutMS=3000, connectTimeoutMS=3000
    )
    try:
        await client.admin.command("ping")
        database = client[os.environ["PI_CLEAR_DB"]]
        conversation_filter = {"project_id": project} if project else {}
        # Turns are removed first. A turn whose conversation is already gone is
        # unreachable, so failing between the two deletes would orphan rows.
        if project:
            ids = [
                document["_id"]
                async for document in database["conversations"].find(
                    conversation_filter, {"_id": 1}
                )
            ]
            turns = await database["turns"].delete_many(
                {"conversation_id": {"$in": ids}}
            )
        else:
            turns = await database["turns"].delete_many({})
        conversations = await database["conversations"].delete_many(conversation_filter)
        print(
            f"Deleted {conversations.deleted_count} conversation(s) "
            f"and {turns.deleted_count} turn(s)."
        )
        print("Collections, indexes and the retention TTL were left in place.")
    finally:
        await client.close()


asyncio.run(main())
PYTHON
