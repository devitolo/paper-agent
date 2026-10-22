#!/usr/bin/env bash
set -Eeuo pipefail

: "${PAPER_MINI_NONPROD_IMAGE:?PAPER_MINI_NONPROD_IMAGE must be an immutable digest}"
[[ "$PAPER_MINI_NONPROD_IMAGE" =~ ^ghcr\.io/devitolo/paper-agent@sha256:[0-9a-f]{64}$ ]] || exit 64

ROOT=${PAPER_MINI_NONPROD_ROOT:-$HOME/workspace/paper-mini-nonprod}
ENV_FILE=${PAPER_MINI_NONPROD_ENV_FILE:-$ROOT/nonprod.env}
COMPOSE_FILE=${PAPER_MINI_NONPROD_COMPOSE_FILE:-${GITHUB_WORKSPACE:-$PWD}/deploy/mini-nonprod/compose.bootstrap.yml}
LOCK_FILE=${PAPER_MINI_NONPROD_LOCK_FILE:-$ROOT/deploy.lock}
CONTAINER=${PAPER_MINI_NONPROD_CONTAINER:-paper-mini-nonprod-app-1}
TIMEOUT=${PAPER_MINI_NONPROD_TIMEOUT:-120}

[[ -d "$ROOT" && -f "$ENV_FILE" && -f "$COMPOSE_FILE" ]] || {
  echo 'Mini non-prod bootstrap is not installed; refusing to create production-like state' >&2; exit 66;
}
[[ -f "$ROOT/NONPROD_ONLY" ]] || { echo 'Missing NONPROD_ONLY guard' >&2; exit 66; }
mkdir -p "$ROOT"
exec 9>"$LOCK_FILE"
flock -n 9 || { echo 'another Mini non-prod deployment is active' >&2; exit 75; }

release="$ROOT/releases/$(date -u +%Y%m%d-%H%M%S)-${GITHUB_RUN_ID:-manual}"
umask 077; mkdir -p "$release"
cp -p "$ENV_FILE" "$release/env.before"

rollback() {
  cp -p "$release/env.before" "$ENV_FILE"
  docker compose --project-name paper-mini-nonprod --env-file "$ENV_FILE" -f "$COMPOSE_FILE" \
    up -d --no-build --pull never app >/dev/null
  printf 'rollback=completed\n' > "$release/result"
}
trap 'rollback || true' ERR

if grep -q '^PAPER_MINI_NONPROD_IMAGE=' "$ENV_FILE"; then
  sed -i "s|^PAPER_MINI_NONPROD_IMAGE=.*$|PAPER_MINI_NONPROD_IMAGE=$PAPER_MINI_NONPROD_IMAGE|" "$ENV_FILE"
else
  printf 'PAPER_MINI_NONPROD_IMAGE=%s\n' "$PAPER_MINI_NONPROD_IMAGE" >> "$ENV_FILE"
fi

docker pull "$PAPER_MINI_NONPROD_IMAGE" >/dev/null
docker compose --project-name paper-mini-nonprod --env-file "$ENV_FILE" -f "$COMPOSE_FILE" \
  up -d --no-build --pull never app >/dev/null

for ((i=0; i<TIMEOUT; i++)); do
  status=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}unknown{{end}}' "$CONTAINER" 2>/dev/null || true)
  if [[ "$status" == healthy ]] && curl --fail --silent --show-error --max-time 5 http://127.0.0.1:18080/ready >/dev/null \
      && curl --fail --silent --show-error --max-time 5 http://127.0.0.1:18080/ | grep -q 'NON-PROD'; then
    printf 'image=%s\nhealth=healthy\nnonprod_badge=present\nrollback=not-needed\n' "$PAPER_MINI_NONPROD_IMAGE" > "$release/result"
    trap - ERR; exit 0
  fi
  sleep 1
done

echo 'Mini non-prod app did not become healthy' >&2
rollback
exit 1
