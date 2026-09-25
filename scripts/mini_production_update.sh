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
STAMP=$(date -u +%Y%m%d-%H%M%S)
RELEASE_DIR=$MINI_ROOT/production-releases/$STAMP

[[ -d "$CANDIDATE_DIR" ]] || { echo "Missing candidate dir: $CANDIDATE_DIR" >&2; exit 1; }
[[ -f "$ENV_FILE" ]] || { echo "Missing production env file: $ENV_FILE" >&2; exit 1; }
[[ -f "$OVERLAY" ]] || { echo "Missing production overlay: $OVERLAY" >&2; exit 1; }

cd "$CANDIDATE_DIR"

compose=(
  docker compose
  --env-file "$ENV_FILE"
  -f docker-compose.mini-migration.yml
  -f "$OVERLAY"
)

mkdir -p "$RELEASE_DIR"
{
  printf 'started_at=%s\n' "$(date -u +%FT%TZ)"
  printf 'candidate_dir=%s\n' "$CANDIDATE_DIR"
  printf 'final_root=%s\n' "$FINAL_ROOT"
  printf 'env_file=%s\n' "$ENV_FILE"
  printf 'overlay=%s\n' "$OVERLAY"
  printf 'new_app_image=%s\n' "$APP_IMAGE"
} > "$RELEASE_DIR/update.env"

"${compose[@]}" ps > "$RELEASE_DIR/before-ps.txt"
docker inspect paper-mini-production-app-1 > "$RELEASE_DIR/before-app-inspect.json" 2>/dev/null || true
crontab -l > "$RELEASE_DIR/crontab.before" 2>/dev/null || true
cp "$ENV_FILE" "$RELEASE_DIR/production.env.before"

old_image=$(docker inspect --format '{{.Config.Image}}' paper-mini-production-app-1 2>/dev/null || true)
old_config_id=$(docker inspect --format '{{.Image}}' paper-mini-production-app-1 2>/dev/null || true)
{
  printf 'old_config_image=%s\n' "$old_image"
  printf 'old_config_id=%s\n' "$old_config_id"
} >> "$RELEASE_DIR/update.env"

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

{
  printf 'completed_at=%s\n' "$(date -u +%FT%TZ)"
  printf 'result=pass\n'
  printf 'rollback_script=%s\n' "$RELEASE_DIR/rollback.sh"
} >> "$RELEASE_DIR/update.env"

echo "Mini production app update complete."
echo "Evidence: $RELEASE_DIR"
echo "Rollback script: $RELEASE_DIR/rollback.sh"
