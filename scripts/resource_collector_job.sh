#!/usr/bin/env bash
set -euo pipefail

# Dedicated host cron entry. This never runs a Paper pipeline job.
root="${PROJECT_PAPER_RESOURCE_ROOT:-/var/lib/project-paper-observability/mini-nonprod}"
collector="${PROJECT_PAPER_RESOURCE_COLLECTOR:-/opt/project-paper/scripts/host_resource_collector.py}"
mkdir -p "$root"
exec /usr/bin/flock -n "$root/collector.lock" \
  /usr/bin/python3 "$collector" collect
