#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${PAPER_AGENT_REPO:-$HOME/workspace/paper-agent}"
cd "$REPO_DIR"

mkdir -p logs

git pull --ff-only

python3 -m paper_agents.cli pipeline-daily \
  --quick \
  --fetch "${PAPER_AGENT_FETCH:-20}" \
  --keep "${PAPER_AGENT_KEEP:-3}" \
  --request-delay "${PAPER_AGENT_REQUEST_DELAY:-5}" \
  --retries "${PAPER_AGENT_RETRIES:-4}" \
  --source-timeout "${PAPER_AGENT_SOURCE_TIMEOUT:-90}"
