#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${PAPER_AGENT_REPO:-$HOME/workspace/paper-agent}"
LOCK_DIR="${PAPER_AGENT_LOCK_DIR:-/tmp}"
LOCK_PATH="$LOCK_DIR/project-paper-semantic-scholar.lock"

cd "$REPO_DIR"
mkdir -p logs "$LOCK_DIR"
exec 9>"$LOCK_PATH"
if ! flock -n 9; then
  echo "Skipping Semantic Scholar pipeline: another run holds $LOCK_PATH."
  exit "${PAPER_AGENT_BUSY_EXIT_CODE:-0}"
fi

python3 -m paper_agents.cli pipeline-daily \
  --source semantic_scholar \
  --topic-slot "${PAPER_AGENT_TOPIC_SLOT:-0}" \
  --quick \
  --fetch "${PAPER_AGENT_SEMANTIC_FETCH:-10}" \
  --keep "${PAPER_AGENT_SEMANTIC_KEEP:-1}" \
  --max-scout-attempts 1 \
  --request-delay "${PAPER_AGENT_SEMANTIC_REQUEST_DELAY:-10}" \
  --retries "${PAPER_AGENT_SEMANTIC_RETRIES:-6}" \
  --source-timeout "${PAPER_AGENT_SEMANTIC_SOURCE_TIMEOUT:-120}"
