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
  pull) python -c "import os,pathlib; from paper_agents.sync_worker import request; from paper_agents.whole_cycle_exchange import pull; r=pathlib.Path(os.environ["PAPER_AGENT_SYNC_ROOT"]); s=pathlib.Path(os.environ["PAPER_AGENT_SYNC_STATE_DIR"]); pull(pathlib.Path(os.environ["PAPER_AGENT_SYNC_DB_PATH"]),r,s,request(os.environ["PAPER_AGENT_SYNC_AWS_URL"],"GET",b"",os.environ["PAPER_AGENT_SYNC_SHARED_SECRET"],"/sync/snapshot"))" ;;
  push) python -c "import os,pathlib; from paper_agents.whole_cycle_exchange import push; push(pathlib.Path(os.environ["PAPER_AGENT_SYNC_DB_PATH"]),pathlib.Path(os.environ["PAPER_AGENT_SYNC_ROOT"]),pathlib.Path(os.environ["PAPER_AGENT_SYNC_STATE_DIR"]),os.environ["PAPER_AGENT_SYNC_AWS_URL"])" ;;
  *) echo 'Usage: mini_sync_exchange.sh pull|push' >&2; exit 64 ;;
esac
