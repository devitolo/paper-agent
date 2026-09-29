#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${PAPER_AGENT_REPO:-$HOME/workspace/paper-agent}"
LOCK_DIR="${PAPER_AGENT_LOCK_DIR:-/tmp}"
LOCK_PATH="$LOCK_DIR/project-paper-core.lock"

cd "$REPO_DIR"
mkdir -p logs "$LOCK_DIR"
exec 9>"$LOCK_PATH"
if ! flock -n 9; then
  echo "Skipping CORE pipeline: another run holds $LOCK_PATH."
  exit "${PAPER_AGENT_BUSY_EXIT_CODE:-0}"
fi

python3 -m paper_agents.cli pipeline-daily \
  --source core \
  --topic "${PAPER_AGENT_CORE_TOPIC_1:-ai platform operations reliability observability production engineering}" \
  --topic "${PAPER_AGENT_CORE_TOPIC_2:-aiops root cause analysis incident management}" \
  --topic "${PAPER_AGENT_CORE_TOPIC_3:-autonomous multi-agent systems software engineering operations}" \
  --topic-slot "${PAPER_AGENT_TOPIC_SLOT:-0}" \
  --quick \
  --fetch "${PAPER_AGENT_CORE_FETCH:-10}" \
  --keep "${PAPER_AGENT_CORE_KEEP:-2}" \
  --max-scout-attempts 1 \
  --request-delay "${PAPER_CORE_REQUEST_DELAY:-3}" \
  --retries 0 \
  --source-timeout "${PAPER_AGENT_CORE_SOURCE_TIMEOUT:-90}"
