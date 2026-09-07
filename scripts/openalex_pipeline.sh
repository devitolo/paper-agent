#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${PAPER_AGENT_REPO:-$HOME/workspace/paper-agent}"
LOCK_DIR="${PAPER_AGENT_LOCK_DIR:-/tmp}"
LOCK_PATH="$LOCK_DIR/project-paper-openalex.lock"
cd "$REPO_DIR"

mkdir -p logs
mkdir -p "$LOCK_DIR"
exec 9>"$LOCK_PATH"
if ! flock -n 9; then
  echo "Skipping OpenAlex pipeline: another run holds $LOCK_PATH."
  exit 0
fi

if [[ "${PAPER_AGENT_SELF_UPDATE:-0}" == "1" ]]; then
  git pull --ff-only
fi

if [[ -n "${PAPER_AGENT_OPENALEX_TOPIC:-}" ]]; then
  topic="$PAPER_AGENT_OPENALEX_TOPIC"
else
  topic="$(python3 -c 'from paper_agents.topic_inventory import openalex_rotating_topic; print(openalex_rotating_topic())')"
fi

echo "OpenAlex topic: $topic"

python3 -m paper_agents.cli pipeline-daily \
  --source openalex \
  --topic "$topic" \
  --quick \
  --fetch "${PAPER_AGENT_OPENALEX_FETCH:-30}" \
  --keep "${PAPER_AGENT_OPENALEX_KEEP:-2}" \
  --max-scout-attempts 1 \
  --request-delay "${PAPER_AGENT_REQUEST_DELAY:-5}" \
  --retries "${PAPER_AGENT_RETRIES:-4}" \
  --source-timeout "${PAPER_AGENT_SOURCE_TIMEOUT:-90}"
