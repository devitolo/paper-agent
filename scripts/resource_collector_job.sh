#!/usr/bin/env bash
set -euo pipefail

# Dedicated host cron entry. This never runs a Paper pipeline job.
root="${PROJECT_PAPER_RESOURCE_ROOT:-/var/lib/project-paper-observability/mini-nonprod}"
collector="${PROJECT_PAPER_RESOURCE_COLLECTOR:-/opt/project-paper/scripts/host_resource_collector.py}"
mkdir -p "$root"
log="$root/collector.log"
if [[ -f "$log" ]] && [[ $(stat -c %s "$log" 2>/dev/null || echo 0) -gt 1048576 ]]; then
  mv -f "$log" "$log.1"
fi
write_state() { local state="$1" file="$root/last-run.json" tmp="$root/.last-run.tmp"; printf '{"state":"%s","observed_at":"%s"}\n' "$state" "$(date -u +%FT%TZ)" > "$tmp"; chmod 0600 "$tmp"; mv -f "$tmp" "$file"; }
set +e
/usr/bin/flock -n "$root/collector.lock" /usr/bin/timeout --signal=TERM --kill-after=5s 45s /usr/bin/python3 "$collector" collect >>"$log" 2>&1
rc=$?
set -e
if [[ $rc -eq 1 ]]; then write_state skipped; exit 0; fi
if [[ $rc -ne 0 ]]; then write_state failed; exit "$rc"; fi
rm -f "$root/last-error.json"
write_state completed
