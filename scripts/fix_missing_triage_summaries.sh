#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${PAPER_AGENT_REPO:-$HOME/workspace/paper-agent}"
DB_PATH="${PAPER_AGENT_DB:-data/paper_agent.db}"

cd "$REPO_DIR"

echo "Backfilling missing triage summaries for recommended papers..."
python3 -m paper_agents.cli review-backfill --db "$DB_PATH" "$@"
