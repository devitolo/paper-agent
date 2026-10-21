#!/usr/bin/env bash
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
lock_file="${PAPER_AGENT_ORCHESTRATOR_LOCK_FILE:-$HOME/paper-mini-rehearsal/production-cutover/orchestrator.lock}"
status_file="${PAPER_AGENT_ORCHESTRATOR_STATUS_FILE:-$HOME/paper-mini-rehearsal/production-cutover/orchestrator-status.json}"
log_dir="${PAPER_AGENT_ORCHESTRATOR_LOG_DIR:-$HOME/paper-mini-rehearsal/logs}"
mkdir -p "$(dirname "$lock_file")" "$(dirname "$status_file")" "$log_dir"
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
payload = {'run_id': run_id, 'input_revision': os.environ.get('PAPER_AGENT_INPUT_REVISION', 'unknown'), 'stage': stage, 'stage_result': result, 'started_at': started, 'updated_at': datetime.now(timezone.utc).isoformat(), 'reason': reason[:160], 'overall_result': 'completed' if stage == 'complete' and result == 'completed' else (result if result in ('failed','skipped') else 'running')}
fd, tmp = tempfile.mkstemp(prefix='.orchestrator-', dir=os.path.dirname(path), text=True)
with os.fdopen(fd, 'w') as out: json.dump(payload, out, sort_keys=True); out.write('\n')
os.replace(tmp, path)
PY
}
run_hook() {
  local name="$1" command="${2:-}"
  [[ -n "$command" ]] || return 0
  local log_file="$log_dir/${run_id}-${name}.log"
  printf '[%s] start %s\n' "$(date -u +%FT%TZ)" "$name" | tee -a "$log_file"
  if ! bash -c "$command" >> "$log_file" 2>&1; then
    printf '[%s] failed %s\n' "$(date -u +%FT%TZ)" "$name" | tee -a "$log_file"
    write_status "$name" failed 'sync hook failed; pipeline was not run'
    return 1
  fi
  printf '[%s] completed %s\n' "$(date -u +%FT%TZ)" "$name" | tee -a "$log_file"
}
pre_sync_command="${PAPER_AGENT_SYNC_PRE_RUN_COMMAND:-}"
post_sync_command="${PAPER_AGENT_SYNC_POST_RUN_COMMAND:-}"
if [[ "${PAPER_AGENT_SYNC_ENABLED:-0}" == 1 ]]; then
  pre_sync_command="${pre_sync_command:-$repo/scripts/mini_sync_exchange.sh pull}"
  post_sync_command="${post_sync_command:-$repo/scripts/mini_sync_exchange.sh push}"
fi
if ! run_hook sync_snapshot "$pre_sync_command"; then exit 1; fi
run_stage() {
  local stage="$1"
  write_status "$stage" running
  local log_file="$log_dir/${run_id}-${stage}.log"
  printf '[%s] start %s\n' "$(date -u +%FT%TZ)" "$stage" | tee -a "$log_file"
  if ! bash "$repo/scripts/mini_container_job.sh" "$stage" >> "$log_file" 2>&1; then
    printf '[%s] failed %s\n' "$(date -u +%FT%TZ)" "$stage" | tee -a "$log_file"
    write_status "$stage" failed 'stage failed; later stages were not run'
    return 1
  fi
  printf '[%s] completed %s\n' "$(date -u +%FT%TZ)" "$stage" | tee -a "$log_file"
  write_status "$stage" completed
}
for stage in openalex arxiv semantic core profile; do
  if ! run_stage "$stage"; then exit 1; fi
done
if ! run_hook sync_pipeline_results "$post_sync_command"; then exit 1; fi
write_status complete completed
