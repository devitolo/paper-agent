#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${PAPER_AGENT_REPO:-$HOME/workspace/paper-agent}"
LOCK_DIR="${PAPER_AGENT_LOCK_DIR:-/tmp}"
LOCK_PATH="$LOCK_DIR/project-paper-arxiv.lock"
cd "$REPO_DIR"

mkdir -p logs
mkdir -p "$LOCK_DIR"
exec 9>"$LOCK_PATH"
if ! flock -n 9; then
  echo "Skipping arXiv pipeline: another run holds $LOCK_PATH."
  exit 0
fi

if [[ "${PAPER_AGENT_SELF_UPDATE:-0}" == "1" ]]; then
  git pull --ff-only
fi

python3 -m paper_agents.cli pipeline-daily \
  --quick \
  --fetch "${PAPER_AGENT_FETCH:-20}" \
  --keep "${PAPER_AGENT_KEEP:-3}" \
  --request-delay "${PAPER_AGENT_REQUEST_DELAY:-5}" \
  --retries "${PAPER_AGENT_RETRIES:-4}" \
  --source-timeout "${PAPER_AGENT_SOURCE_TIMEOUT:-90}"
