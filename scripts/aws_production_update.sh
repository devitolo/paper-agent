#!/usr/bin/env bash
set -Eeuo pipefail

# Small, AWS-host-only release step. It pulls an already qualified immutable
# image and recreates only the existing app service over its existing volumes.
# The caller must run this as root (the Compose file and production env are
# intentionally root-owned).

AWS_APP_IMAGE=${1:-${AWS_APP_IMAGE:-}}
: "${AWS_APP_IMAGE:?AWS_APP_IMAGE must be an immutable registry digest}"
[[ "$AWS_APP_IMAGE" =~ ^ghcr\.io/devitolo/paper-agent@sha256:[0-9a-f]{64}$ ]] || {
  echo 'AWS_APP_IMAGE must be a lowercase GHCR sha256 digest' >&2; exit 64;
}

PROJECT_DIR=${AWS_PROJECT_DIR:-/home/ubuntu/project-paper-prod}
ENV_FILE=${AWS_ENV_FILE:-$PROJECT_DIR/.env}
COMPOSE_FILE=${AWS_COMPOSE_FILE:-$PROJECT_DIR/compose.yml}
RESOURCE_OVERRIDE=$PROJECT_DIR/compose.aws-resources.json
compose() {
  local extra=()
  [[ ! -L "$RESOURCE_OVERRIDE" ]] || { echo "resource override symlink refused" >&2; return 75; }
  [[ ! -f "$RESOURCE_OVERRIDE" ]] || extra=(-f "$RESOURCE_OVERRIDE")
  docker compose --project-directory "$PROJECT_DIR" --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "${extra[@]}" "$@"
}
SERVICE=${AWS_SERVICE:-app}
CONTAINER=${AWS_CONTAINER:-project-paper-prod-app-1}
RELEASE_ROOT=${AWS_RELEASE_ROOT:-$PROJECT_DIR/production-releases}
LOCK_FILE=${AWS_DEPLOY_LOCK_FILE:-$PROJECT_DIR/deploy.lock}
HEALTH_URL=${AWS_HEALTH_URL:-http://127.0.0.1:8000/ready}
TIMEOUT=${AWS_HEALTH_TIMEOUT_SECONDS:-180}

[[ $EUID -eq 0 ]] || { echo 'must run as root' >&2; exit 77; }
[[ -f "$ENV_FILE" && -f "$COMPOSE_FILE" ]] || { echo 'AWS Compose state is missing' >&2; exit 66; }
mkdir -p "$RELEASE_ROOT"
exec 9>"$LOCK_FILE"
flock -n 9 || { echo 'another AWS deployment is active' >&2; exit 75; }

release="$RELEASE_ROOT/$(date -u +%Y%m%d-%H%M%S)-${GITHUB_RUN_ID:-manual}"
umask 077
mkdir "$release"
cp -p "$ENV_FILE" "$release/env.before"
cp -p "$COMPOSE_FILE" "$release/compose.before"
[[ ! -L "$RESOURCE_OVERRIDE" ]] || { echo "resource override symlink refused" >&2; exit 75; }
if [[ -f "$RESOURCE_OVERRIDE" ]]; then cp -p "$RESOURCE_OVERRIDE" "$release/resource.before"; fi
printf '%s\n' "$AWS_APP_IMAGE" > "$release/image"

restore() {
  cp -p "$release/env.before" "$ENV_FILE"
  cp -p "$release/compose.before" "$COMPOSE_FILE"
  if [[ -f "$release/resource.before" ]]; then cp -p "$release/resource.before" "$RESOURCE_OVERRIDE"; else rm -f "$RESOURCE_OVERRIDE"; fi
  compose up -d --no-build --pull never --no-deps "$SERVICE" >/dev/null
}
failed=0
trap 'failed=1; restore || true; printf "rollback=attempted\\n" > "$release/result"' ERR

tmp_env=$(mktemp "$release/env.XXXXXX")
cp -p "$ENV_FILE" "$tmp_env"
if grep -q '^PAPER_APP_IMAGE=' "$tmp_env"; then
  sed -i "s|^PAPER_APP_IMAGE=.*$|PAPER_APP_IMAGE=$AWS_APP_IMAGE|" "$tmp_env"
else
  printf 'PAPER_APP_IMAGE=%s\n' "$AWS_APP_IMAGE" >> "$tmp_env"
fi
cp -p "$tmp_env" "$ENV_FILE"

# Connect an already-installed AWS collector; never create or replace its data.
# Back up consistently before adding the read-only mount. Existing secrets and
# volume definitions are retained by the narrow Compose transformation.
if [[ -f /var/lib/project-paper-observability/aws-production-export/status.json ]]; then
  backup_name="$(basename "$release").db"
  docker exec "$CONTAINER" python3 -c 'import sqlite3,sys; src=sqlite3.connect("file:/app/data/paper_agent.db?mode=ro",uri=True); dst=sqlite3.connect("/backups/"+sys.argv[1]); src.backup(dst); dst.close(); src.close()' "$backup_name"
  printf 'database_backup=/backups/%s\n' "$backup_name" > "$release/backup-evidence"
  docker compose --project-directory "$PROJECT_DIR" --env-file "$ENV_FILE" -f "$COMPOSE_FILE" config --format json | python3 "$(dirname -- "$0")/aws_resource_status_mount.py" "$RESOURCE_OVERRIDE"
  compose config --quiet
fi

docker pull "$AWS_APP_IMAGE" >/dev/null
compose up -d --no-build --pull never --no-deps "$SERVICE" >/dev/null

for ((i=0; i<TIMEOUT; i++)); do
  status=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}unknown{{end}}' "$CONTAINER" 2>/dev/null || true)
  if [[ "$status" == healthy ]] && curl --fail --silent --show-error --max-time 5 "$HEALTH_URL" >/dev/null; then
    printf 'image=%s\nhealth=healthy\nrollback=not-needed\n' "$AWS_APP_IMAGE" > "$release/result"
    trap - ERR
    exit 0
  fi
  sleep 1
done

echo "AWS app did not become healthy within ${TIMEOUT}s" >&2
restore
printf 'rollback=completed\n' > "$release/result"
exit 1
