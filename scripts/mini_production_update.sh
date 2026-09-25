#!/usr/bin/env bash
# User-run Mini production app image update. Does not rebuild on production.
set -euo pipefail
umask 077

usage() {
  cat >&2 <<'EOF'
Usage:
  PAPER_MINI_APP_IMAGE=ghcr.io/devitolo/paper-agent@sha256:... \
  bash scripts/mini_production_update.sh

Optional:
  PAPER_MINI_ROOT=/home/devitolo/paper-mini-rehearsal
  PAPER_MINI_FINAL_ROOT=/home/devitolo/paper-mini-rehearsal/production-cutover/final-...
  PAPER_MINI_PRODUCTION_OVERLAY=/home/devitolo/paper-mini-rehearsal/production-cutover/docker-compose.production.yml
  PAPER_MINI_CANDIDATE_DIR=/home/devitolo/paper-mini-rehearsal/candidate
  PAPER_MINI_DEPLOY_LOCK_FILE=/home/devitolo/paper-mini-rehearsal/production-cutover/deploy.lock
  PAPER_MINI_DRAIN_TIMEOUT=1800
  PAPER_MINI_DEPLOY_BRANCH=mini-production
EOF
}

APP_IMAGE=${PAPER_MINI_APP_IMAGE:-}
[[ -n "$APP_IMAGE" ]] || { usage; exit 64; }
[[ "$APP_IMAGE" =~ ^[a-zA-Z0-9][a-zA-Z0-9._/:@-]*@sha256:[a-f0-9]{64}$ ]] \
  || { echo "PAPER_MINI_APP_IMAGE must be immutable image@sha256:digest" >&2; exit 64; }

MINI_ROOT=${PAPER_MINI_ROOT:-$HOME/paper-mini-rehearsal}
FINAL_ROOT=${PAPER_MINI_FINAL_ROOT:-$MINI_ROOT/production-cutover/final-20260924-230636}
ENV_FILE=${PAPER_MINI_ENV_FILE:-$FINAL_ROOT/production.env}
OVERLAY=${PAPER_MINI_PRODUCTION_OVERLAY:-$MINI_ROOT/production-cutover/docker-compose.production.yml}
CANDIDATE_DIR=${PAPER_MINI_CANDIDATE_DIR:-$MINI_ROOT/candidate}
DEPLOY_LOCK_FILE=${PAPER_MINI_DEPLOY_LOCK_FILE:-$MINI_ROOT/production-cutover/deploy.lock}
DRAIN_TIMEOUT=${PAPER_MINI_DRAIN_TIMEOUT:-1800}
DEPLOY_BRANCH=${PAPER_MINI_DEPLOY_BRANCH:-mini-production}
STAMP=$(date -u +%Y%m%d-%H%M%S)
RELEASE_DIR=$MINI_ROOT/production-releases/$STAMP
DEPLOYED_MARKER=$MINI_ROOT/production-current/app-image.ref
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"

[[ -d "$CANDIDATE_DIR" ]] || { echo "Missing candidate dir: $CANDIDATE_DIR" >&2; exit 1; }
[[ -f "$ENV_FILE" ]] || { echo "Missing production env file: $ENV_FILE" >&2; exit 1; }
[[ -f "$OVERLAY" ]] || { echo "Missing production overlay: $OVERLAY" >&2; exit 1; }
[[ "$DRAIN_TIMEOUT" =~ ^[0-9]+$ && "$DRAIN_TIMEOUT" -gt 0 ]] || { echo "PAPER_MINI_DRAIN_TIMEOUT must be a positive integer" >&2; exit 64; }

cd "$CANDIDATE_DIR"

compose=(
  docker compose
  --env-file "$ENV_FILE"
  -f docker-compose.mini-migration.yml
  -f "$OVERLAY"
)

mkdir -p "$(dirname "$DEPLOY_LOCK_FILE")"
exec 9>"$DEPLOY_LOCK_FILE"
if ! flock -n -x 9; then
  echo "Another Project Paper deployment is already running." >&2
  exit 75
fi

if [[ -n "${GITHUB_SHA:-}" && -n "${GITHUB_REF_NAME:-}" && "$GITHUB_REF_NAME" == "$DEPLOY_BRANCH" ]]; then
  if git -C "$SOURCE_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git -C "$SOURCE_DIR" fetch --quiet origin "$DEPLOY_BRANCH"
    latest=$(git -C "$SOURCE_DIR" rev-parse "origin/$DEPLOY_BRANCH")
    if [[ "$latest" != "$GITHUB_SHA" ]]; then
      echo "Skipping stale deployment: $GITHUB_SHA is no longer origin/$DEPLOY_BRANCH ($latest)."
      exit 0
    fi
  fi
fi

mkdir -p "$RELEASE_DIR"
mkdir -p "$CANDIDATE_DIR/scripts"
install -m 0644 "$SOURCE_DIR/docker-compose.mini-migration.yml" "$CANDIDATE_DIR/docker-compose.mini-migration.yml"
install -m 0644 "$SOURCE_DIR/docker-compose.mini-rehearsal.yml" "$CANDIDATE_DIR/docker-compose.mini-rehearsal.yml"
install -m 0755 "$SOURCE_DIR/scripts/mini_container_job.sh" "$CANDIDATE_DIR/scripts/mini_container_job.sh"
install -m 0755 "$SOURCE_DIR/scripts/mini_production_update.sh" "$CANDIDATE_DIR/scripts/mini_production_update.sh"
{
  printf 'started_at=%s\n' "$(date -u +%FT%TZ)"
  printf 'candidate_dir=%s\n' "$CANDIDATE_DIR"
  printf 'final_root=%s\n' "$FINAL_ROOT"
  printf 'env_file=%s\n' "$ENV_FILE"
  printf 'overlay=%s\n' "$OVERLAY"
  printf 'new_app_image=%s\n' "$APP_IMAGE"
  printf 'deploy_lock_file=%s\n' "$DEPLOY_LOCK_FILE"
  printf 'drain_timeout=%s\n' "$DRAIN_TIMEOUT"
} > "$RELEASE_DIR/update.env"

"${compose[@]}" ps > "$RELEASE_DIR/before-ps.txt"
docker inspect paper-mini-production-app-1 > "$RELEASE_DIR/before-app-inspect.json" 2>/dev/null || true
crontab -l > "$RELEASE_DIR/crontab.before" 2>/dev/null || true
cp "$ENV_FILE" "$RELEASE_DIR/production.env.before"

cat > "$RELEASE_DIR/rollback.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd "$CANDIDATE_DIR"
cp "$RELEASE_DIR/production.env.before" "$ENV_FILE"
crontab "$RELEASE_DIR/crontab.before"
compose=(docker compose --env-file "$ENV_FILE" -f docker-compose.mini-migration.yml)
if grep -q '^PAPER_REHEARSAL_SOURCE_SHA256=' "$ENV_FILE"; then
  compose+=(-f docker-compose.mini-rehearsal.yml)
fi
compose+=(-f "$OVERLAY")
"\${compose[@]}" up -d --no-deps --pull never --force-recreate app
"\${compose[@]}" exec -T app python -m paper_agents.package_runtime check-app
EOF
chmod 700 "$RELEASE_DIR/rollback.sh"

old_image=$(docker inspect --format '{{.Config.Image}}' paper-mini-production-app-1 2>/dev/null || true)
old_config_id=$(docker inspect --format '{{.Image}}' paper-mini-production-app-1 2>/dev/null || true)
old_env_image=$(sed -n 's/^PAPER_MIGRATION_APP_IMAGE=//p' "$ENV_FILE" | tail -1)
{
  printf 'old_config_image=%s\n' "$old_image"
  printf 'old_config_id=%s\n' "$old_config_id"
  printf 'old_env_image=%s\n' "$old_env_image"
} >> "$RELEASE_DIR/update.env"

if [[ "$old_env_image" == "$APP_IMAGE" ]]; then
  echo "Desired image is already configured: $APP_IMAGE"
  mkdir -p "$(dirname "$DEPLOYED_MARKER")"
  printf '%s\n' "$APP_IMAGE" > "$DEPLOYED_MARKER"
  exit 0
fi

docker pull "$APP_IMAGE"
docker image inspect "$APP_IMAGE" > "$RELEASE_DIR/new-app-image-inspect.json"
python3 - "$RELEASE_DIR/new-app-image-inspect.json" <<'PY'
from __future__ import annotations

import json
import sys

value = json.loads(open(sys.argv[1], encoding="utf-8").read())
labels = value[0].get("Config", {}).get("Labels", {}) or {}
if labels.get("org.projectpaper.runtime") != "mini-production":
    raise SystemExit("selected image is not the Mini production runtime")
if labels.get("org.opencontainers.image.source") != "https://github.com/devitolo/paper-agent":
    raise SystemExit("selected image is not from Project Paper")
PY

tmp_env=$RELEASE_DIR/production.env.next
python3 - "$ENV_FILE" "$tmp_env" "$APP_IMAGE" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
image = sys.argv[3]
lines = source.read_text(encoding="utf-8").splitlines()
found = False
out: list[str] = []
drop_prefixes = ("PAPER_REHEARSAL_SOURCE_SHA256=", "PAPER_REHEARSAL_IDENTITY_FILE=")
for line in lines:
    if line.startswith(drop_prefixes):
        continue
    if line.startswith("PAPER_MIGRATION_APP_IMAGE="):
        out.append(f"PAPER_MIGRATION_APP_IMAGE={image}")
        found = True
    else:
        out.append(line)
if not found:
    out.append(f"PAPER_MIGRATION_APP_IMAGE={image}")
destination.write_text("\n".join(out) + "\n", encoding="utf-8")
PY

chmod 600 "$tmp_env"
cp "$tmp_env" "$ENV_FILE"

if crontab -l > "$RELEASE_DIR/crontab.current" 2>/dev/null; then
  python3 - "$RELEASE_DIR/crontab.current" "$RELEASE_DIR/crontab.next" "$OVERLAY" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
overlay = sys.argv[3]
out: list[str] = []
found = False
for line in source.read_text(encoding="utf-8").splitlines():
    if line.startswith("PAPER_MIGRATION_EXTRA_COMPOSE_FILES="):
        out.append(f"PAPER_MIGRATION_EXTRA_COMPOSE_FILES={overlay}")
        found = True
    else:
        out.append(line)
if not found:
    raise SystemExit("managed cron is missing PAPER_MIGRATION_EXTRA_COMPOSE_FILES")
destination.write_text("\n".join(out) + "\n", encoding="utf-8")
PY
  crontab "$RELEASE_DIR/crontab.next"
fi

"${compose[@]}" config --quiet

echo "Waiting for active app jobs to finish before deployment."
deadline=$((SECONDS + DRAIN_TIMEOUT))
until "${compose[@]}" exec -T app python - <<'PY'
from pathlib import Path
from paper_agents.migration_lifecycle import lease
import os
with lease(Path(os.environ["PAPER_AGENT_LIFECYCLE_DIR"]), exclusive=True):
    pass
PY
do
  [[ "$SECONDS" -lt "$deadline" ]] || {
    echo "Timed out waiting for active Project Paper jobs to finish. No container replacement attempted." >&2
    exit 75
  }
  sleep 10
done

"${compose[@]}" exec -T app bash scripts/backup_db.sh /app/data/paper_agent.db /backups \
  > "$RELEASE_DIR/pre-update-db-backup.log"

"${compose[@]}" up -d --no-deps --pull never --force-recreate app

deadline=$((SECONDS + 120))
until "${compose[@]}" exec -T app python -m paper_agents.package_runtime check-app >/dev/null 2>&1; do
  [[ "$SECONDS" -lt "$deadline" ]] || {
    "${compose[@]}" logs --no-color --tail=200 app > "$RELEASE_DIR/app-readiness-failure.log" 2>&1 || true
    echo "App readiness failed. Evidence: $RELEASE_DIR" >&2
    exit 1
  }
  sleep 3
done

curl --fail --silent --show-error http://127.0.0.1:8000/ > "$RELEASE_DIR/home.html"
"${compose[@]}" exec -T app python -m paper_agents.package_runtime check-model \
  > "$RELEASE_DIR/qwen-check.json"
"${compose[@]}" ps > "$RELEASE_DIR/after-ps.txt"
docker inspect paper-mini-production-app-1 > "$RELEASE_DIR/after-app-inspect.json" 2>/dev/null || true
mkdir -p "$(dirname "$DEPLOYED_MARKER")"
printf '%s\n' "$APP_IMAGE" > "$DEPLOYED_MARKER"

{
  printf 'completed_at=%s\n' "$(date -u +%FT%TZ)"
  printf 'result=pass\n'
  printf 'rollback_script=%s\n' "$RELEASE_DIR/rollback.sh"
} >> "$RELEASE_DIR/update.env"

echo "Mini production app update complete."
echo "Evidence: $RELEASE_DIR"
echo "Rollback script: $RELEASE_DIR/rollback.sh"
if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
  {
    echo "## Project Paper Mini deployment"
    echo
    echo "- Result: success"
    echo "- Image: \`$APP_IMAGE\`"
    echo "- Evidence: \`$RELEASE_DIR\`"
    echo "- Rollback: \`$RELEASE_DIR/rollback.sh\`"
  } >> "$GITHUB_STEP_SUMMARY"
fi
