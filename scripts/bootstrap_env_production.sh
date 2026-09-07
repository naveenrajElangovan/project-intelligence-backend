#!/usr/bin/env bash
# Create the three .env.production files the production and release compose files
# require, from each repo's own .example. Without them `docker compose up` fails
# on a missing required env file before any container starts -- which reads as
# "docker is not up".
#
#   ./scripts/bootstrap_env_production.sh          # report what is missing
#   ./scripts/bootstrap_env_production.sh --write  # create from the examples
#
# Nothing secret is invented: the placeholders from the .example files are copied
# verbatim and every line still needing a real value is listed for you to fill.
set -euo pipefail

WRITE="${1:-}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SIBLINGS="$(dirname "$HERE")"

TARGETS=(
  "$HERE"
  "$SIBLINGS/project-intelligence-rag"
  "$SIBLINGS/project-intelligence-ingestion"
)

missing_values=0
for repo in "${TARGETS[@]}"; do
  name="$(basename "$repo")"
  target="$repo/.env.production"
  example="$repo/.env.production.example"
  if [[ ! -f "$example" ]]; then
    echo "  SKIP    $name: no .env.production.example"
    continue
  fi
  if [[ -f "$target" ]]; then
    echo "  EXISTS  $name/.env.production"
  elif [[ "$WRITE" == "--write" ]]; then
    cp "$example" "$target"
    chmod 600 "$target"
    echo "  CREATED $name/.env.production  (mode 600, from the example)"
  else
    echo "  MISSING $name/.env.production  -- run with --write to create it"
    continue
  fi
  # Report the lines that still hold a placeholder rather than a real value.
  while IFS= read -r line; do
    key="${line%%=*}"
    value="${line#*=}"
    case "$value" in
      ""|"load-from-azure-key-vault"|*"<"*">"*|*"YOUR-"*|*"your-"*|*".example"*)
        echo "      fill: $key"
        missing_values=$((missing_values + 1))
        ;;
    esac
  done < <(grep -E '^[A-Z][A-Z0-9_]*=' "$target" || true)
done

echo
if [[ "$WRITE" != "--write" ]]; then
  echo "Report only. Re-run with --write to create the files."
elif (( missing_values > 0 )); then
  echo "$missing_values value(s) above still need filling before the stack will start."
  echo "The compose files read these at container start, not at build time."
else
  echo "All three files present with no placeholders left."
fi
