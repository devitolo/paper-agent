#!/bin/sh
set -eu
env_file="${PROJECT_PAPER_RESOURCE_ENV_FILE:-/etc/project-paper-observability/aws-production/aws-production.env}"
if [ -r "$env_file" ]; then . "$env_file"; fi
# host-package-pending-guard-v1 (caller holds collector lock)
pending=/var/lib/project-paper-observability/mini-production-control/HOST-PACKAGE-PENDING.json
if [ "${PROJECT_PAPER_RESOURCE_ENVIRONMENT:-}" = mini-production ] && [ "${PROJECT_PAPER_HOST_PACKAGE_QUALIFY:-0}" != 1 ] && { [ -e "$pending" ] || [ -L "$pending" ]; }; then
  echo 'host package recovery pending' >&2
  exit 75
fi
image="${PROJECT_PAPER_OBSERVABILITY_IMAGE:?image digest is required}"
case "$image" in *[!A-Za-z0-9_./:@-]*) echo 'immutable image digest is required' >&2; exit 64 ;; esac
printf '%s' "$image" | /usr/bin/grep -Eq '^[^@[:space:]]+@sha256:[0-9a-f]{64}$' || { echo 'immutable image digest is required' >&2; exit 64; }
container_name="project-paper-observability-collector"
state="${PROJECT_PAPER_RESOURCE_ROOT:?state path is required}"
export_dir="${PROJECT_PAPER_RESOURCE_EXPORT_DIR:?export path is required}"
mkdir -p "$state" "$export_dir"
log="$state/collector.log"
if [ -f "$log" ] && [ "$(wc -c <"$log")" -gt 1048576 ]; then
  : > "$log.1"
  tail -c 524288 "$log" > "$log.1"
  : > "$log"
fi
cleanup() { rc=$?; if [ "$rc" -ne 0 ]; then /usr/bin/timeout --signal=TERM --kill-after=1s 5s docker rm -f "$container_name" >/dev/null 2>&1 || true; fi; exit "$rc"; }
trap cleanup EXIT
/usr/bin/timeout --signal=TERM --kill-after=5s 45s docker run --rm --name "$container_name" \
  --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
  --memory 64m --cpus 0.10 --pids-limit 32 --tmpfs /tmp:rw,noexec,nosuid,size=8m \
  -e PROJECT_PAPER_RESOURCE_ROOT=/state \
  -e PROJECT_PAPER_RESOURCE_EXPORT_DIR=/export \
  -e PROJECT_PAPER_RESOURCE_ENVIRONMENT="${PROJECT_PAPER_RESOURCE_ENVIRONMENT:-mini-production}" \
  -e PROJECT_PAPER_RESOURCE_HOST="${PROJECT_PAPER_RESOURCE_HOST:-unknown}" \
  -e PROJECT_PAPER_RESOURCE_PROC_ROOT=/host/proc \
  -e PROJECT_PAPER_RESOURCE_SYS_ROOT=/host/sys \
  -e PROJECT_PAPER_RESOURCE_CGROUP_ROOT=/sys/fs/cgroup \
  -e PROJECT_PAPER_RESOURCE_BOOT_ID_PATH=/host/boot_id \
  -e PROJECT_PAPER_RESOURCE_DISK_PATH=/state \
  -v "$state:/state:rw" -v "$export_dir:/export:rw" \
  -v /proc/stat:/host/proc/stat:ro \
  -v /proc/meminfo:/host/proc/meminfo:ro \
  -v /proc/vmstat:/host/proc/vmstat:ro \
  -v /proc/sys/kernel/random/boot_id:/host/boot_id:ro \
  -v /sys/class/thermal:/host/sys/class/thermal:ro \
  -v /sys/fs/cgroup:/sys/fs/cgroup:ro \
  "$image"
