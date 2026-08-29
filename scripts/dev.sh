#!/usr/bin/env bash
# Start the local client tiers independently. PostgreSQL + cloud can be
# started with `docker compose up -d postgres cloud`.
#
#   scripts/dev.sh backend   # server  -> http://127.0.0.1:8000
#   scripts/dev.sh cloud     # cloud   -> http://127.0.0.1:8100 (local dev)
#   scripts/dev.sh frontend  # client  -> http://localhost:5173
#   scripts/dev.sh tui       # client  -> terminal (needs backend running)
#   scripts/dev.sh all       # backend + frontend together
#
# Model config defaults to ~/.mini_agent/config.toml; override with MINI_AGENT_CONFIG.
set -euo pipefail
cd "$(dirname "$0")/.."

CONFIG="${MINI_AGENT_CONFIG:-$HOME/.mini_agent/config.toml}"

start_backend() {
  echo "[dev] backend server: http://127.0.0.1:8000  (config: $CONFIG)"
  MINI_AGENT_CONFIG="$CONFIG" uv run python -m backend.api
}

start_frontend() {
  echo "[dev] frontend client: http://localhost:5173"
  (cd frontend && npm run dev)
}

start_cloud() {
  echo "[dev] cloud API: http://127.0.0.1:8100 (PostgreSQL must be running)"
  uv run --package mini-agent-cloud python -m cloud
}

start_tui() {
  echo "[dev] tui client (network mode). Backend must be running first."
  uv run python run.py "$@"
}

case "${1:-all}" in
  backend)
    start_backend
    ;;
  frontend)
    start_frontend
    ;;
  cloud)
    start_cloud
    ;;
  tui)
    shift
    start_tui "$@"
    ;;
  all)
    echo "[dev] starting backend + frontend. Run the TUI in a separate terminal: scripts/dev.sh tui"
    trap 'kill 0' EXIT INT TERM
    start_backend &
    sleep 1
    start_frontend &
    wait
    ;;
  *)
    echo "usage: scripts/dev.sh [backend|cloud|frontend|tui|all]"
    exit 1
    ;;
esac
