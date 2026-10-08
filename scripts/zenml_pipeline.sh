#!/usr/bin/env bash
set -euo pipefail
ROOT="${PAPER_AGENT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
LOCK_DIR="${PAPER_AGENT_LOCK_DIR:-/tmp}"
mkdir -p "$LOCK_DIR"
exec 9>"$LOCK_DIR/project-paper-zenml.lock"
if ! flock -n 9; then
  echo "Skipping ZenML: another run holds project-paper-zenml.lock."
  exit "${PAPER_AGENT_BUSY_EXIT_CODE:-0}"
fi
cd "$ROOT"
exec python -m paper_agents.cli zenml-pilot --db data/paper_agent.db
