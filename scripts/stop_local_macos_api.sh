#!/usr/bin/env bash
set -euo pipefail

# The backend had no stop script, so the documented way to stop it was
# `kill "$(cat .run/backend-api.pid)"` — which fails silently once the pid file
# outlives its process, leaving an orphan holding the port. This trusts the port.

SCRIPT_DIRECTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIRECTORY="$(cd "${SCRIPT_DIRECTORY}/.." && pwd)"
PID_FILE="${PROJECT_DIRECTORY}/.run/backend-api.pid"
PORT="${PI_BACKEND_LOCAL_PORT:-8001}"

listeners() {
  lsof -ti "tcp:${PORT}" -sTCP:LISTEN 2>/dev/null || true
}

targets=""
if [[ -f "${PID_FILE}" ]]; then
  recorded="$(tr -cd '0-9' <"${PID_FILE}")"
  if [[ -n "${recorded}" ]] && kill -0 "${recorded}" 2>/dev/null; then
    targets="${recorded}"
  fi
fi
for pid in $(listeners); do
  case " ${targets} " in
    *" ${pid} "*) ;;
    *) targets="${targets} ${pid}" ;;
  esac
done
targets="$(echo "${targets}" | xargs || true)"

if [[ -z "${targets}" ]]; then
  echo "No local backend process is running on port ${PORT}."
else
  for pid in ${targets}; do
    kill "${pid}" 2>/dev/null || true
  done
  for _ in {1..20}; do
    [[ -z "$(listeners)" ]] && break
    sleep 0.5
  done
  if [[ -n "$(listeners)" ]]; then
    for pid in $(listeners); do
      kill -9 "${pid}" 2>/dev/null || true
    done
    sleep 1
  fi
  if [[ -n "$(listeners)" ]]; then
    echo "Port ${PORT} is still held after SIGKILL by: $(listeners | xargs)" >&2
    echo "The pid file was left in place so the state stays inspectable." >&2
    exit 1
  fi
  echo "Stopped local backend (pid(s) ${targets})."
fi

# MongoDB is deliberately left running; the launcher reuses a healthy container.
rm -f "${PID_FILE}"
