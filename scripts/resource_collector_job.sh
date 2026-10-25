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
if ! /usr/bin/flock -n "$root/collector.lock" /usr/bin/timeout --signal=TERM --kill-after=5s 45s /usr/bin/python3 "$collector" collect >>"$log" 2>&1; then
  printf '{"state":"error","observed_at":"%s","error":"collection_failed"}\n' "$(date -u +%FT%TZ)" > "$root/last-error.json"
  exit 1
fi
