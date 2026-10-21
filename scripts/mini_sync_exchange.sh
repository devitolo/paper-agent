#!/usr/bin/env bash
set -euo pipefail
[[ "${PAPER_AGENT_SYNC_ENABLED:-0}" == 1 ]] || { echo 'Mini AWS sync disabled'; exit 0; }
: "${PAPER_AGENT_SYNC_AWS_URL:?Set PAPER_AGENT_SYNC_AWS_URL}"
: "${PAPER_AGENT_SYNC_SHARED_SECRET:?Set PAPER_AGENT_SYNC_SHARED_SECRET}"
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
db_path="${PAPER_AGENT_SYNC_DB_PATH:-$repo/data/paper_agent.db}"
state_dir="${PAPER_AGENT_SYNC_STATE_DIR:-$repo/data/sync-state}"
root="${PAPER_AGENT_SYNC_ROOT:-$repo}"
environment="${PAPER_AGENT_SYNC_ENVIRONMENT:-production}"
producer="${PAPER_AGENT_SYNC_PRODUCER:-mini-production}"
case "${1:-}" in
  pull|push) python -m paper_agents.whole_cycle_exchange "$1" --db-path "$db_path" --root "$root" --state-dir "$state_dir" --aws-url "$PAPER_AGENT_SYNC_AWS_URL" ;;
  *) echo 'Usage: mini_sync_exchange.sh pull|push' >&2; exit 64 ;;
esac
