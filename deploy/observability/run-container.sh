#!/bin/sh
set -eu
image="${PROJECT_PAPER_OBSERVABILITY_IMAGE:?image digest is required}"
state="${PROJECT_PAPER_RESOURCE_ROOT:?state path is required}"
export_dir="${PROJECT_PAPER_RESOURCE_EXPORT_DIR:?export path is required}"
mkdir -p "$state" "$export_dir"
exec docker run --rm --name project-paper-observability-collector \
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
