#!/usr/bin/env bash
set -euo pipefail

# Recover the provider mappings from the ingestion state table and re-seed the
# local control-plane project record with them.
#
# Two repositories are involved because the two halves of the answer live apart:
# the mapping shape is reconstructible from ingestion manifests (Azure Table,
# still readable while Azure SQL is paused), and the record it has to be written
# into belongs to the backend. Doing it in one script keeps the merge honest --
# the recovered arrays replace only themselves, and vectorStore, ingestionSchedule
# and displayName are preserved from the existing description file.
#
#   ./scripts/repair_local_project.sh
#   ./scripts/repair_local_project.sh --project DEMO --dry-run

SCRIPT_DIRECTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIRECTORY="$(cd "${SCRIPT_DIRECTORY}/.." && pwd)"
INGESTION_DIRECTORY="$(cd "${PROJECT_DIRECTORY}/../project-intelligence-ingestion" && pwd)"
PROJECT="DEMO"
DRY_RUN=false
PROJECT_FILE=""

while (($#)); do
  case "$1" in
    --project) PROJECT="${2:?--project needs a value}"; shift 2 ;;
    --project-file) PROJECT_FILE="${2:?--project-file needs a path}"; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done
PROJECT_FILE="${PROJECT_FILE:-${PROJECT_DIRECTORY}/config/local-project.${PROJECT}.json}"

for candidate in "${PROJECT_DIRECTORY}/.venv/bin/python" "${INGESTION_DIRECTORY}/.venv/bin/python"; do
  if [[ ! -x "${candidate}" ]]; then
    echo "Missing virtual environment: ${candidate}" >&2
    exit 1
  fi
done
if [[ ! -f "${PROJECT_FILE}" ]]; then
  echo "${PROJECT_FILE} does not exist." >&2
  exit 1
fi

echo "== Recovering mappings from the ingestion state table"
RECOVERED="$(cd "${INGESTION_DIRECTORY}" && .venv/bin/python -m scripts.recover_project_mappings \
  --project "${PROJECT}" | sed -n '/^{/,/^}/p')"
if [[ -z "${RECOVERED}" ]]; then
  echo "The recovery script produced no JSON; nothing was changed." >&2
  exit 1
fi

echo "${RECOVERED}"
echo
echo "== Merging into ${PROJECT_FILE}"
MERGED="$("${PROJECT_DIRECTORY}/.venv/bin/python" - "${PROJECT_FILE}" "${DRY_RUN}" <<'MERGE'
import json
import pathlib
import sys

path, dry_run = pathlib.Path(sys.argv[1]), sys.argv[2] == "true"
description = json.loads(path.read_text(encoding="utf-8"))
recovered = json.load(sys.stdin)

for key in ("githubRepositories", "jiraProjects", "confluenceSpaces"):
    value = recovered.get(key)
    if not value:
        # An empty recovery is not evidence of an empty mapping -- the partition
        # may simply have been purged -- so the existing value is kept.
        print(f"kept existing {key}: recovery found nothing", file=sys.stderr)
        continue
    if key == "confluenceSpaces":
        # spaceKey and rootPageIds are not recoverable from a scope, so carry
        # forward whatever the file already had for the same spaceId.
        existing = {str(item.get("spaceId")): item for item in description.get(key) or []}
        for item in value:
            previous = existing.get(item["spaceId"], {})
            item["spaceKey"] = previous.get("spaceKey") or item["spaceKey"]
            item["rootPageIds"] = previous.get("rootPageIds") or item["rootPageIds"]
    description[key] = value

if not dry_run:
    path.write_text(json.dumps(description, indent=2) + "\n", encoding="utf-8")
print(json.dumps(description, indent=2))
MERGE
<<<"${RECOVERED}")"
echo "${MERGED}"

if [[ "${DRY_RUN}" == "true" ]]; then
  echo
  echo "Dry run: ${PROJECT_FILE} was not written and the record was not re-seeded."
  exit 0
fi

echo
echo "== Re-seeding the control-plane record"
cd "${PROJECT_DIRECTORY}"
.venv/bin/python -m scripts.seed_project_record "${PROJECT_FILE}"

echo
echo "Mappings restored. Restart the backend for the new record to be read:"
echo "  ./scripts/prepare_and_start_local_app.sh"
echo
echo "If the chat still says it is unavailable, that is integration_connections,"
echo "not the mapping: connect Atlassian once in the app. can_ask_questions is"
echo "project.active AND at least one available integration."
