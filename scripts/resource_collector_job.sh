#!/usr/bin/env bash
set -euo pipefail

# Dedicated host cron entry. This never runs a Paper pipeline job.
root="${PROJECT_PAPER_RESOURCE_ROOT:-/var/lib/project-paper-observability/mini-nonprod}"
collector="${PROJECT_PAPER_RESOURCE_COLLECTOR:-/opt/project-paper/scripts/host_resource_collector.py}"
env_file="${PROJECT_PAPER_RESOURCE_ENV_FILE:-/etc/project-paper-observability/mini-production/mini-production.env}"
if [[ -f "$env_file" ]]; then set -a; . "$env_file"; set +a; fi
[[ -n "${PROJECT_PAPER_RESOURCE_ENVIRONMENT:-}" ]] || { echo 'collector environment is not configured' >&2; exit 64; }
mkdir -p "$root"
log="$root/collector.log"
if [[ -f "$log" ]] && [[ $(stat -c %s "$log" 2>/dev/null || echo 0) -gt 1048576 ]]; then
  mv -f "$log" "$log.1"
fi
write_state() { local state="$1" file="$root/last-run.json" tmp="$root/.last-run.tmp"; printf '{"state":"%s","observed_at":"%s"}\n' "$state" "$(date -u +%FT%TZ)" > "$tmp"; chmod 0600 "$tmp"; mv -f "$tmp" "$file"; }
exec 9>"$root/collector.lock"
if ! /usr/bin/flock -n 9; then write_state skipped; exit 0; fi
set +e
/usr/bin/timeout --signal=TERM --kill-after=5s 45s /usr/bin/python3 "$collector" collect >>"$log" 2>&1
rc=$?
set -e
if [[ $rc -ne 0 ]]; then write_state failed; exit "$rc"; fi
rm -f "$root/last-error.json"
write_state completed
