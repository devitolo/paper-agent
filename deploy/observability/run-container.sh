#!/bin/sh
set -eu
env_file="${PROJECT_PAPER_RESOURCE_ENV_FILE:-/etc/project-paper-observability/mini-production/mini-production.env}"
if [ -r "$env_file" ]; then . "$env_file"; fi
image="${PROJECT_PAPER_OBSERVABILITY_IMAGE:?image digest is required}"
case "$image" in *[!A-Za-z0-9_./:@-]*) echo 'immutable image digest is required' >&2; exit 64 ;; esac
printf '%s' "$image" | /usr/bin/grep -Eq '^[^@[:space:]]+@sha256:[0-9a-f]{64}$' || { echo 'immutable image digest is required' >&2; exit 64; }
state="${PROJECT_PAPER_RESOURCE_ROOT:?state path is required}"
export_dir="${PROJECT_PAPER_RESOURCE_EXPORT_DIR:?export path is required}"
mkdir -p "$state" "$export_dir"
log="$state/collector.log"
if [ -f "$log" ] && [ "$(wc -c <"$log")" -gt 1048576 ]; then mv -f "$log" "$log.1"; fi
exec /usr/bin/timeout --signal=TERM --kill-after=5s 45s docker run --rm --name project-paper-observability-collector \
  --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
  --memory 64m --cpus 0.10 --pids-limit 32 --tmpfs /tmp:rw,noexec,nosuid,size=8m \
  -e PROJECT_PAPER_RESOURCE_ROOT=/state \
  -e PROJECT_PAPER_RESOURCE_EXPORT_DIR=/export \
  -e PROJECT_PAPER_RESOURCE_ENVIRONMENT="${PROJECT_PAPER_RESOURCE_ENVIRONMENT:-mini-production}" \
  -e PROJECT_PAPER_RESOURCE_HOST="${PROJECT_PAPER_RESOURCE_HOST:-unknown}" \
  -e PROJECT_PAPER_RESOURCE_PROC_ROOT=/host/proc \
  -e PROJECT_PAPER_RESOURCE_SYS_ROOT=/host/sys \
  -e PROJECT_PAPER_RESOURCE_CGROUP_ROOT=/sys/fs/cgroup \
  -v "$state:/state:rw" -v "$export_dir:/export:rw" \
  -v /proc/stat:/host/proc/stat:ro \
  -v /proc/meminfo:/host/proc/meminfo:ro \
  -v /proc/vmstat:/host/proc/vmstat:ro \
  -v /sys/class/thermal:/host/sys/class/thermal:ro \
  -v /sys/fs/cgroup:/sys/fs/cgroup:ro \
  "$image"
