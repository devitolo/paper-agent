#!/bin/sh
set -eu
env_file="${PROJECT_PAPER_RESOURCE_ENV_FILE:-/etc/project-paper-observability/aws-production/aws-production.env}"
if [ -r "$env_file" ]; then . "$env_file"; fi
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

# AWS-local container facts follow successful host collection. No socket is
# mounted: fixed, bounded host CLI queries feed the same isolated image on stdin.
if [ "${PROJECT_PAPER_RESOURCE_ENVIRONMENT:-}" = aws-production ]; then
  aws_facts() {
    for container in project-paper-prod-app-1 project-paper-observability-collector; do
      inspected=$(/usr/bin/timeout 3s docker inspect --format '{"status":{{json .State.Status}},"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}"no-healthcheck"{{end}},"image_id":{{json .Image}}}' "$container" 2>/dev/null) || inspected=''
      if [ -z "$inspected" ]; then inspected='{}'; fi
      printf '%s\n' "$inspected"
      image_id=$(/usr/bin/timeout 3s docker inspect --format '{{.Image}}' "$container" 2>/dev/null) || image_id=''
      digests='{}'
      if printf '%s' "$image_id" | /usr/bin/grep -Eq '^sha256:[0-9a-f]{64}$'; then
        digests=$(/usr/bin/timeout 3s docker image inspect --format '{"image_id":{{json .Id}},"digests":{{json .RepoDigests}}}' "$image_id" 2>/dev/null) || digests='{}'
        if [ "$digests" = null ] || [ -z "$digests" ]; then digests='{}'; fi
      fi
      printf '%s\n' "$digests"
      stats=$(/usr/bin/timeout 4s docker stats --no-stream --format '{"cpu":{{json .CPUPerc}},"memory":{{json .MemUsage}}}' "$container" 2>/dev/null) || stats=''
      if [ -z "$stats" ]; then stats='{}'; fi
      printf '%s\n' "$stats"
    done
  }
  snapshot_name="project-paper-observability-aws-facts-$$"
  if ! aws_facts | /usr/bin/timeout --signal=TERM --kill-after=2s 40s docker run --rm -i --pull=never \
    --name "$snapshot_name" --log-driver=none --network=none --read-only \
    --cap-drop=ALL --security-opt=no-new-privileges --user=10001:10001 \
    --memory=32m --memory-swap=32m --cpus=0.10 --pids-limit=16 \
    --mount "type=bind,source=$export_dir,target=/export" \
    --entrypoint=python "$image" -m paper_agents.aws_container_status; then
      /usr/bin/timeout --signal=TERM --kill-after=1s 5s docker rm -f "$snapshot_name" >/dev/null 2>&1 || true
      echo 'aws_container_snapshot=unavailable' >&2
  fi
fi
