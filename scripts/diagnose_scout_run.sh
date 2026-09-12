#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${PAPER_AGENT_REPO:-$HOME/workspace/paper-agent}"
DB_PATH="${PAPER_AGENT_DB:-data/paper_agent.db}"
SOURCE="${1:-}"

if [[ -z "$SOURCE" ]]; then
  echo "Usage: scripts/diagnose_scout_run.sh <source>" >&2
  exit 2
fi

cd "$REPO_DIR"
# The pipeline automatically saves reports using this same diagnostic collector.
# Prefer --run-id via the Python module when investigating a particular warning.
python3 -m paper_agents.scout_diagnostics --db "$DB_PATH" --source "$SOURCE"
