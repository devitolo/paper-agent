#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${PAPER_AGENT_REPO:-$HOME/workspace/paper-agent}"
DB_PATH="${PAPER_AGENT_DB:-data/paper_agent.db}"
MODEL="${PAPER_AGENT_GEMINI_MODEL:-}"

cd "$REPO_DIR"

mode="--dry-run"
if [[ "${1:-}" == "--apply" ]]; then
  mode=""
  shift
elif [[ "${1:-}" == "--dry-run" ]]; then
  shift
fi

args=(feedback apply --provider gemini --db "$DB_PATH")
if [[ -n "$MODEL" ]]; then
  args+=(--model "$MODEL")
fi
if [[ -n "$mode" ]]; then
  args+=("$mode")
fi
args+=("$@")

if [[ -n "$mode" ]]; then
  echo "Previewing pending structured feedback profile apply..."
else
  echo "Applying pending structured feedback to the active profile..."
fi

python3 -m paper_agents.cli "${args[@]}"
