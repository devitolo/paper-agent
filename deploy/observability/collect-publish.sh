#!/usr/bin/env bash
# Called by the existing Mini cron under the collector lock.
set -Eeuo pipefail
# host-package-pending-guard-v1 (caller holds collector lock)
[[ ! -e /var/lib/project-paper-observability/mini-production-control/HOST-PACKAGE-PENDING.json && ! -L /var/lib/project-paper-observability/mini-production-control/HOST-PACKAGE-PENDING.json ]] || { echo 'host package recovery pending' >&2; exit 75; }
set -a
source /etc/project-paper-observability/mini-production/mini-production.env
set +a
[[ $PROJECT_PAPER_RESOURCE_ENVIRONMENT == mini-production ]] || exit 64
/usr/local/sbin/project-paper-observability-container
config=/etc/project-paper-observability/mini-production/publisher.env
[[ -r $config ]] || { echo 'publisher_disabled=config_missing'; exit 0; }
source "$config"
[[ ${PAPER_OBSERVABILITY_PUBLISH_ENABLED:-0} == 1 ]] || { echo 'publisher_disabled=flag'; exit 0; }
[[ $PROJECT_PAPER_PUBLISHER_IMAGE =~ ^ghcr\.io/devitolo/paper-agent/observability-publisher@sha256:[0-9a-f]{64}$ ]] || exit 64
[[ $PROJECT_PAPER_PUBLISHER_URL == https://paper-prod.tail5fc461.ts.net/observability/v1/mini-status ]] || exit 64
[[ $PROJECT_PAPER_PUBLISHER_DNS == 100.100.100.100 ]] || exit 64
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
/usr/bin/timeout --signal=TERM --kill-after=5s 35s docker run --rm --pull=never \
  --name "$name" --log-driver=none --dns="$PROJECT_PAPER_PUBLISHER_DNS" \
  --read-only --cap-drop=ALL --security-opt=no-new-privileges \
  --user=10002:10002 --memory=64m --memory-swap=64m --cpus=0.10 \
  --pids-limit=16 --tmpfs /tmp:rw,noexec,nosuid,size=4m \
  -e PAPER_OBSERVABILITY_PUBLISH_ENABLED=1 \
  --mount type=bind,source=/var/lib/project-paper-observability/mini-production-export,target=/export,readonly \
  --mount type=bind,source=/var/lib/project-paper-observability/mini-production-publisher,target=/state \
  --mount type=bind,source=/etc/project-paper-observability/mini-production/transport.key,target=/key,readonly \
  "$PROJECT_PAPER_PUBLISHER_IMAGE" \
  --status-file /export/status.json --state-file /state/pending.json \
  --url "$PROJECT_PAPER_PUBLISHER_URL" --key-file /key
