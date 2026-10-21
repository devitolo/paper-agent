#!/usr/bin/env bash
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
lock_file="${PAPER_AGENT_ORCHESTRATOR_LOCK_FILE:-$HOME/paper-mini-rehearsal/production-cutover/orchestrator.lock}"
status_file="${PAPER_AGENT_ORCHESTRATOR_STATUS_FILE:-$HOME/paper-mini-rehearsal/production-cutover/orchestrator-status.json}"
mkdir -p "$(dirname "$lock_file")" "$(dirname "$status_file")"
exec 9>"$lock_file"
if ! flock -n 9; then exit 0; fi
run_id="$(date -u +%Y%m%dT%H%M%SZ)-$$"
started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
write_status() {
  local stage="$1" result="$2" reason="${3:-}"
  python3 - "$status_file" "$run_id" "$started" "$stage" "$result" "$reason" <<'PY'
import json, os, sys, tempfile
from datetime import datetime, timezone
path, run_id, started, stage, result, reason = sys.argv[1:]
payload = {'run_id': run_id, 'input_revision': os.environ.get('PAPER_AGENT_INPUT_REVISION', 'unknown'), 'stage': stage, 'stage_result': result, 'started_at': started, 'updated_at': datetime.now(timezone.utc).isoformat(), 'reason': reason[:160], 'overall_result': result if result in ('failed','skipped') else 'running'}
fd, tmp = tempfile.mkstemp(prefix='.orchestrator-', dir=os.path.dirname(path), text=True)
with os.fdopen(fd, 'w') as out: json.dump(payload, out, sort_keys=True); out.write('\n')
os.replace(tmp, path)
PY
}
run_stage() {
  local stage="$1"
  write_status "$stage" running
  if [[ "$stage" == sync ]]; then
    [[ -n "${PAPER_AGENT_SYNC_COMMAND:-}" ]] || { write_status sync skipped 'sync adapter is not configured'; return 75; }
    bash -c "$PAPER_AGENT_SYNC_COMMAND"
  else
    bash "$repo/scripts/mini_container_job.sh" "$stage"
  fi
  write_status "$stage" completed
}
if ! run_stage sync; then write_status sync skipped 'sync failed; discovery and profile stages were not run'; exit 0; fi
for stage in openalex arxiv semantic core profile; do
  if ! run_stage "$stage"; then write_status "$stage" failed 'stage failed; later stages were not run'; exit 1; fi
done
write_status complete completed
