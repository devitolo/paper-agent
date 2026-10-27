#!/usr/bin/env bash
# Called by the existing Mini cron under the collector lock.
set -Eeuo pipefail
set -a
source /etc/project-paper-observability/mini-production/mini-production.env
set +a
[[ $PROJECT_PAPER_RESOURCE_ENVIRONMENT == mini-production ]] || exit 64
/usr/local/sbin/project-paper-observability-container
config=/etc/project-paper-observability/mini-production/publisher.env
[[ -r $config ]] || { echo 'publisher_disabled=config_missing'; exit 0; }
source "$config"
[[ ${PAPER_OBSERVABILITY_PUBLISH_ENABLED:-0} == 1 ]] || { echo 'publisher_disabled=flag'; exit 0; }
publisher_image=$PROJECT_PAPER_OBSERVABILITY_IMAGE
[[ $publisher_image =~ ^ghcr\.io/devitolo/paper-agent/observability:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}$ ]] || exit 64
[[ $PROJECT_PAPER_PUBLISHER_URL == https://paper-prod.tail5fc461.ts.net/observability/v1/mini-status ]] || exit 64
[[ $PROJECT_PAPER_PUBLISHER_DNS == 100.100.100.100 ]] || exit 64
# Host Docker queries are fixed and bounded. Only approved facts enter stdin;
# neither container receives Docker socket, host root, config, or log access.
container_health() {
  local container inspected image_id digests stats
  for container in paper-mini-production-app-1 paper-minilm-production-minilm-1 paper-mini-production-ollama-1 paper-observability-phoenix-1 project-paper-observability-collector project-paper-observability-publisher; do
    inspected=$(/usr/bin/timeout 3s docker inspect --format '{"status":{{json .State.Status}},"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}"no-healthcheck"{{end}},"image_id":{{json .Image}}}' "$container" 2>/dev/null) || inspected=''
    if [[ -z $inspected ]]; then inspected='{}'; fi
    printf '%s\n' "$inspected"
    image_id=$(/usr/bin/timeout 3s docker inspect --format '{{.Image}}' "$container" 2>/dev/null) || image_id=''
    digests='{}'
    if [[ $image_id =~ ^sha256:[0-9a-f]{64}$ ]]; then
      digests=$(/usr/bin/timeout 3s docker image inspect --format '{"image_id":{{json .Id}},"digests":{{json .RepoDigests}}}' "$image_id" 2>/dev/null) || digests='{}'
      [[ $digests != null ]] || digests='{}'
    fi
    if [[ -z $digests ]]; then digests='{}'; fi
    printf '%s\n' "$digests"
    stats=$(/usr/bin/timeout 4s docker stats --no-stream --format '{"cpu":{{json .CPUPerc}},"memory":{{json .MemUsage}}}' "$container" 2>/dev/null) || stats=''
    if [[ -z $stats ]]; then stats='{}'; fi
    printf '%s\n' "$stats"
  done
}
name=project-paper-observability-publisher
if docker container inspect "$name" >/dev/null 2>&1; then
  echo 'publisher_failed=existing_container' >&2
  exit 75
fi
cleanup() {
  rc=$?
  trap - EXIT
  /usr/bin/timeout --signal=TERM --kill-after=1s 5s docker rm -f "$name" >/dev/null 2>&1 || true
  exit "$rc"
}
trap cleanup EXIT
container_health | /usr/bin/timeout --signal=TERM --kill-after=5s 100s docker run --rm -i --pull=never \
  --name "$name" --log-driver=none --dns="$PROJECT_PAPER_PUBLISHER_DNS" \
  --read-only --cap-drop=ALL --security-opt=no-new-privileges \
  --user=10002:10002 --entrypoint=/opt/project-paper-observability/publish.sh --memory=64m --memory-swap=64m --cpus=0.10 \
  --pids-limit=16 --tmpfs /tmp:rw,noexec,nosuid,size=4m \
  -e PAPER_OBSERVABILITY_PUBLISH_ENABLED=1 \
  --mount type=bind,source=/var/lib/project-paper-observability/mini-production-export,target=/export,readonly \
  --mount type=bind,source=/var/lib/project-paper-observability/mini-production-publisher,target=/state \
  --mount type=bind,source=/etc/project-paper-observability/mini-production/transport.key,target=/key,readonly \
  "$publisher_image" \
  --container-health-stdin --status-file /export/status.json --state-file /state/pending.json \
  --url "$PROJECT_PAPER_PUBLISHER_URL" --key-file /key
