#!/bin/sh
set -eu
# Cron supplies an exact path. A direct invocation selects only an explicit
# target or the sole installed role; never silently choose AWS on a Mini host.
env_file="${PROJECT_PAPER_RESOURCE_ENV_FILE:-}"
if [ -z "$env_file" ]; then
  case "${PROJECT_PAPER_OBSERVABILITY_TARGET:-}" in
    mini-production|aws-production)
      target=$PROJECT_PAPER_OBSERVABILITY_TARGET
      env_file="/etc/project-paper-observability/$target/$target.env" ;;
    '')
      for target in mini-production aws-production; do
        candidate="/etc/project-paper-observability/$target/$target.env"
        if [ -r "$candidate" ]; then
          [ -z "$env_file" ] || { echo 'multiple observability roles; explicit target required' >&2; exit 64; }
          env_file=$candidate
        fi
      done ;;
    *) echo 'unsupported observability target' >&2; exit 64 ;;
  esac
fi
if [ -n "$env_file" ] && [ -r "$env_file" ]; then . "$env_file"; fi
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
