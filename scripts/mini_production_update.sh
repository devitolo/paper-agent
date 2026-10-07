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
  PAPER_MINI_ARXIV_PROGRESS=0|1   # optional persistent production flag update
  PAPER_MINI_OPENALEX_CURSOR=0|1   # optional persistent production flag update
  PAPER_MINI_SEMANTIC_SCHOLAR_PROGRESS=0|1   # optional persistent production flag update
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
ARXIV_PROGRESS=${PAPER_MINI_ARXIV_PROGRESS:-}
OPENALEX_CURSOR=${PAPER_MINI_OPENALEX_CURSOR:-}
SEMANTIC_SCHOLAR_PROGRESS=${PAPER_MINI_SEMANTIC_SCHOLAR_PROGRESS:-}
STAMP=$(date -u +%Y%m%d-%H%M%S)
RELEASE_DIR=$MINI_ROOT/production-releases/$STAMP
DEPLOYED_MARKER=$MINI_ROOT/production-current/app-image.ref
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
DEPLOY_STARTED_EPOCH=$(date +%s)
CURRENT_PHASE=
CURRENT_PHASE_STARTED_EPOCH=0

phase_key() {
  printf '%s' "$1" | tr '[:upper:] -' '[:lower:]__' | tr -cd 'a-z0-9_'
}

phase_start() {
  CURRENT_PHASE=$1
  CURRENT_PHASE_STARTED_EPOCH=$(date +%s)
  echo "Starting deploy phase: $CURRENT_PHASE"
}

phase_end() {
  local ended duration key
  ended=$(date +%s)
  duration=$((ended - CURRENT_PHASE_STARTED_EPOCH))
  key=$(phase_key "$CURRENT_PHASE")
  printf '%s\t%s\n' "$CURRENT_PHASE" "$duration" >> "$RELEASE_DIR/timing.tsv"
  printf 'timing_%s_seconds=%s\n' "$key" "$duration" >> "$RELEASE_DIR/update.env"
  echo "Finished deploy phase: $CURRENT_PHASE (${duration}s)"
}

[[ -d "$CANDIDATE_DIR" ]] || { echo "Missing candidate dir: $CANDIDATE_DIR" >&2; exit 1; }
[[ -f "$ENV_FILE" ]] || { echo "Missing production env file: $ENV_FILE" >&2; exit 1; }
[[ -f "$OVERLAY" ]] || { echo "Missing production overlay: $OVERLAY" >&2; exit 1; }
[[ "$DRAIN_TIMEOUT" =~ ^[0-9]+$ && "$DRAIN_TIMEOUT" -gt 0 ]] || { echo "PAPER_MINI_DRAIN_TIMEOUT must be a positive integer" >&2; exit 64; }
[[ -z "$ARXIV_PROGRESS" || "$ARXIV_PROGRESS" =~ ^[01]$ ]] || { echo "PAPER_MINI_ARXIV_PROGRESS must be 0 or 1 when set" >&2; exit 64; }
[[ -z "$OPENALEX_CURSOR" || "$OPENALEX_CURSOR" =~ ^[01]$ ]] || { echo "PAPER_MINI_OPENALEX_CURSOR must be 0 or 1 when set" >&2; exit 64; }
[[ -z "$SEMANTIC_SCHOLAR_PROGRESS" || "$SEMANTIC_SCHOLAR_PROGRESS" =~ ^[01]$ ]] || { echo "PAPER_MINI_SEMANTIC_SCHOLAR_PROGRESS must be 0 or 1 when set" >&2; exit 64; }

# Credentials in the private production env file are authoritative. Docker Compose
# otherwise gives an ambient shell variable precedence over --env-file.
unset SEMANTIC_SCHOLAR_API_KEY

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
printf 'phase\tseconds\n' > "$RELEASE_DIR/timing.tsv"
phase_start "prepare release files"
mkdir -p "$CANDIDATE_DIR/scripts"
install -m 0644 "$SOURCE_DIR/docker-compose.mini-migration.yml" "$CANDIDATE_DIR/docker-compose.mini-migration.yml"
install -m 0644 "$SOURCE_DIR/docker-compose.mini-rehearsal.yml" "$CANDIDATE_DIR/docker-compose.mini-rehearsal.yml"
install -m 0755 "$SOURCE_DIR/scripts/mini_container_job.sh" "$CANDIDATE_DIR/scripts/mini_container_job.sh"
install -m 0755 "$SOURCE_DIR/scripts/minilm_eval_after_pipeline.sh" "$CANDIDATE_DIR/scripts/minilm_eval_after_pipeline.sh"
install -m 0755 "$SOURCE_DIR/scripts/minilm_shadow_after_pipeline.sh" "$CANDIDATE_DIR/scripts/minilm_shadow_after_pipeline.sh"
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
  printf 'arxiv_progress=%s\n' "${ARXIV_PROGRESS:-preserve}"
  printf 'openalex_cursor=%s\n' "${OPENALEX_CURSOR:-preserve}"
  printf 'semantic_scholar_progress=%s\n' "${SEMANTIC_SCHOLAR_PROGRESS:-preserve}"
} > "$RELEASE_DIR/update.env"

"${compose[@]}" ps > "$RELEASE_DIR/before-ps.txt"
docker inspect paper-mini-production-app-1 > "$RELEASE_DIR/before-app-inspect.json" 2>/dev/null || true
crontab -l > "$RELEASE_DIR/crontab.before" 2>/dev/null || true
cp "$ENV_FILE" "$RELEASE_DIR/production.env.before"

cat > "$RELEASE_DIR/rollback.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd "$CANDIDATE_DIR"
unset SEMANTIC_SCHOLAR_API_KEY
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

DEPLOYMENT_MUTATED=0
APP_REPLACEMENT_STARTED=0
DEPLOYMENT_SUCCESS=0
RESTORE_IN_PROGRESS=0

restore_previous_production() {
  local status=$1
  [[ "$DEPLOYMENT_SUCCESS" == 0 ]] || return 0
  [[ "$DEPLOYMENT_MUTATED" == 1 ]] || return 0
  [[ "$RESTORE_IN_PROGRESS" == 0 ]] || return 0
  RESTORE_IN_PROGRESS=1

  echo "Deployment failed; restoring previous production configuration." >&2
  cp "$RELEASE_DIR/production.env.before" "$ENV_FILE"
  if [[ -f "$RELEASE_DIR/crontab.before" ]]; then
    crontab "$RELEASE_DIR/crontab.before" || true
  fi
  {
    printf 'failed_at=%s\n' "$(date -u +%FT%TZ)"
    printf 'result=failed\n'
    printf 'restore_attempted=true\n'
    printf 'restore_reason=deployment exited %s\n' "$status"
  } >> "$RELEASE_DIR/update.env"

  if [[ "$APP_REPLACEMENT_STARTED" == 1 ]]; then
    local restore_compose
    restore_compose=(docker compose --env-file "$ENV_FILE" -f docker-compose.mini-migration.yml)
    if grep -q '^PAPER_REHEARSAL_SOURCE_SHA256=' "$ENV_FILE"; then
      restore_compose+=(-f docker-compose.mini-rehearsal.yml)
    fi
    restore_compose+=(-f "$OVERLAY")

    if "${restore_compose[@]}" up -d --no-deps --pull never --force-recreate app \
      > "$RELEASE_DIR/auto-restore-app.log" 2>&1; then
      local deadline
      deadline=$((SECONDS + 120))
      until "${restore_compose[@]}" exec -T app python -m paper_agents.package_runtime check-app \
        >> "$RELEASE_DIR/auto-restore-app.log" 2>&1; do
        if [[ "$SECONDS" -ge "$deadline" ]]; then
          "${restore_compose[@]}" logs --no-color --tail=200 app \
            > "$RELEASE_DIR/auto-restore-readiness-failure.log" 2>&1 || true
          echo "Automatic restore attempted but app readiness failed. Evidence: $RELEASE_DIR" >&2
          printf 'restore_result=readiness_failed\n' >> "$RELEASE_DIR/update.env"
          return 0
        fi
        sleep 3
      done
      echo "Previous production app restored after failed deployment." >&2
      printf 'restore_result=pass\n' >> "$RELEASE_DIR/update.env"
    else
      echo "Automatic restore command failed. Evidence: $RELEASE_DIR" >&2
      printf 'restore_result=compose_failed\n' >> "$RELEASE_DIR/update.env"
    fi
  else
    echo "Previous production app was not replaced; restored env/crontab only." >&2
    printf 'restore_result=config_only\n' >> "$RELEASE_DIR/update.env"
  fi
}

on_exit() {
  local status=$?
  if [[ "$status" != 0 ]]; then
    restore_previous_production "$status"
  fi
}
trap on_exit EXIT

old_image=$(docker inspect --format '{{.Config.Image}}' paper-mini-production-app-1 2>/dev/null || true)
old_config_id=$(docker inspect --format '{{.Image}}' paper-mini-production-app-1 2>/dev/null || true)
old_env_image=$(sed -n 's/^PAPER_MIGRATION_APP_IMAGE=//p' "$ENV_FILE" | tail -1)
old_ollama_image=$(sed -n 's/^PAPER_MIGRATION_OLLAMA_IMAGE=//p' "$ENV_FILE" | tail -1)
if [[ -n "$old_config_id" ]]; then
  docker image inspect "$old_config_id" > "$RELEASE_DIR/previous-app-image-inspect.json" 2>/dev/null || true
fi
{
  printf 'old_config_image=%s\n' "$old_image"
  printf 'old_config_id=%s\n' "$old_config_id"
  printf 'old_env_image=%s\n' "$old_env_image"
  printf 'old_ollama_image=%s\n' "$old_ollama_image"
} >> "$RELEASE_DIR/update.env"
phase_end

if [[ "$old_env_image" == "$APP_IMAGE" ]]; then
  echo "Desired image is already configured: $APP_IMAGE"
  mkdir -p "$(dirname "$DEPLOYED_MARKER")"
  printf '%s\n' "$APP_IMAGE" > "$DEPLOYED_MARKER"
  exit 0
fi

phase_start "pull image"
docker pull "$APP_IMAGE"
phase_end

phase_start "inspect image"
docker image inspect "$APP_IMAGE" > "$RELEASE_DIR/new-app-image-inspect.json"
python3 - "$RELEASE_DIR/new-app-image-inspect.json" "$RELEASE_DIR/previous-app-image-inspect.json" "$RELEASE_DIR/image-metadata.env" "$RELEASE_DIR/image-layer-diff.txt" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

value = json.loads(open(sys.argv[1], encoding="utf-8").read())
image = value[0]
labels = image.get("Config", {}).get("Labels", {}) or {}
if labels.get("org.projectpaper.runtime") != "mini-production":
    raise SystemExit("selected image is not the Mini production runtime")
if labels.get("org.opencontainers.image.source") != "https://github.com/devitolo/paper-agent":
    raise SystemExit("selected image is not from Project Paper")
size_bytes = int(image.get("Size") or 0)
layers = image.get("RootFS", {}).get("Layers") or []
try:
    old_value = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
    old_image = old_value[0] if old_value else {}
except (FileNotFoundError, json.JSONDecodeError):
    old_image = {}
old_layers = old_image.get("RootFS", {}).get("Layers") or []
old_layer_set = set(old_layers)
new_layer_set = set(layers)
reused_layers = [layer for layer in layers if layer in old_layer_set]
new_layers = [layer for layer in layers if layer not in old_layer_set]
removed_layers = [layer for layer in old_layers if layer not in new_layer_set]
Path(sys.argv[3]).write_text(
    f"image_size_bytes={size_bytes}\n"
    f"image_size_mib={size_bytes / 1024 / 1024:.1f}\n"
    f"image_layer_count={len(layers)}\n"
    f"previous_image_layer_count={len(old_layers)}\n"
    f"reused_image_layer_count={len(reused_layers)}\n"
    f"new_image_layer_count={len(new_layers)}\n"
    f"removed_image_layer_count={len(removed_layers)}\n",
    encoding="utf-8",
)
Path(sys.argv[4]).write_text(
    "new_layers\n"
    + "\n".join(new_layers)
    + "\n\nremoved_layers\n"
    + "\n".join(removed_layers)
    + "\n",
    encoding="utf-8",
)
PY
cat "$RELEASE_DIR/image-metadata.env" >> "$RELEASE_DIR/update.env"
phase_end

phase_start "prepare env"
ollama_image="$old_ollama_image"
if [[ "$ollama_image" == sha256:* ]]; then
  ollama_image=$(docker image inspect "$ollama_image" --format '{{index .RepoDigests 0}}')
fi
[[ "$ollama_image" =~ ^[a-zA-Z0-9][a-zA-Z0-9._/:@-]*@sha256:[a-f0-9]{64}$ ]] \
  || { echo "PAPER_MIGRATION_OLLAMA_IMAGE must resolve to image@sha256:digest" >&2; exit 64; }
printf 'next_ollama_image=%s\n' "$ollama_image" >> "$RELEASE_DIR/update.env"

tmp_env=$RELEASE_DIR/production.env.next
python3 - "$ENV_FILE" "$tmp_env" "$APP_IMAGE" "$ollama_image" "$ARXIV_PROGRESS" "$OPENALEX_CURSOR" "$SEMANTIC_SCHOLAR_PROGRESS" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
image = sys.argv[3]
ollama_image = sys.argv[4]
arxiv_progress = sys.argv[5]
openalex_cursor = sys.argv[6]
semantic_scholar_progress = sys.argv[7]
lines = source.read_text(encoding="utf-8").splitlines()
found = False
found_ollama = False
found_arxiv_progress = False
found_openalex_cursor = False
found_semantic_scholar_progress = False
out: list[str] = []
drop_prefixes = ("PAPER_REHEARSAL_SOURCE_SHA256=", "PAPER_REHEARSAL_IDENTITY_FILE=")
for line in lines:
    if line.startswith(drop_prefixes):
        continue
    if line.startswith("PAPER_MIGRATION_APP_IMAGE="):
        out.append(f"PAPER_MIGRATION_APP_IMAGE={image}")
        found = True
    elif line.startswith("PAPER_MIGRATION_OLLAMA_IMAGE="):
        out.append(f"PAPER_MIGRATION_OLLAMA_IMAGE={ollama_image}")
        found_ollama = True
    elif line.startswith("PAPER_ARXIV_PROGRESS=") and arxiv_progress:
        out.append(f"PAPER_ARXIV_PROGRESS={arxiv_progress}")
        found_arxiv_progress = True
    elif line.startswith("PAPER_OPENALEX_CURSOR=") and openalex_cursor:
        out.append(f"PAPER_OPENALEX_CURSOR={openalex_cursor}")
        found_openalex_cursor = True
    elif line.startswith("PAPER_SEMANTIC_SCHOLAR_PROGRESS=") and semantic_scholar_progress:
        out.append(f"PAPER_SEMANTIC_SCHOLAR_PROGRESS={semantic_scholar_progress}")
        found_semantic_scholar_progress = True
    else:
        out.append(line)
if not found:
    out.append(f"PAPER_MIGRATION_APP_IMAGE={image}")
if not found_ollama:
    out.append(f"PAPER_MIGRATION_OLLAMA_IMAGE={ollama_image}")
if arxiv_progress and not found_arxiv_progress:
    out.append(f"PAPER_ARXIV_PROGRESS={arxiv_progress}")
if openalex_cursor and not found_openalex_cursor:
    out.append(f"PAPER_OPENALEX_CURSOR={openalex_cursor}")
if semantic_scholar_progress and not found_semantic_scholar_progress:
    out.append(f"PAPER_SEMANTIC_SCHOLAR_PROGRESS={semantic_scholar_progress}")
destination.write_text("\n".join(out) + "\n", encoding="utf-8")
PY

chmod 600 "$tmp_env"
cp "$tmp_env" "$ENV_FILE"
DEPLOYMENT_MUTATED=1

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
found_minilm_eval = False
found_minilm_shadow = False
for line in source.read_text(encoding="utf-8").splitlines():
    if line.startswith("PAPER_MIGRATION_EXTRA_COMPOSE_FILES="):
        out.append(f"PAPER_MIGRATION_EXTRA_COMPOSE_FILES={overlay}")
        found = True
    elif line.startswith("PAPER_MINILM_EVAL_ENABLED="):
        out.append("PAPER_MINILM_EVAL_ENABLED=0")
        found_minilm_eval = True
    elif line.startswith("PAPER_MINILM_SHADOW_ENABLED="):
        out.append("PAPER_MINILM_SHADOW_ENABLED=0")
        found_minilm_shadow = True
    else:
        out.append(line)
if not found:
    raise SystemExit("managed cron is missing PAPER_MIGRATION_EXTRA_COMPOSE_FILES")
if not found_minilm_eval:
    insert_at = next(
        (index + 1 for index, line in enumerate(out) if line.startswith("PAPER_MIGRATION_EXTRA_COMPOSE_FILES=")),
        None,
    )
    if insert_at is None:
        raise SystemExit("cannot place PAPER_MINILM_EVAL_ENABLED in managed cron")
    out.insert(insert_at, "PAPER_MINILM_EVAL_ENABLED=0")
if not found_minilm_shadow:
    insert_at = next(
        (index + 1 for index, line in enumerate(out) if line.startswith("PAPER_MINILM_EVAL_ENABLED=")),
        None,
    )
    if insert_at is None:
        raise SystemExit("cannot place PAPER_MINILM_SHADOW_ENABLED in managed cron")
    out.insert(insert_at, "PAPER_MINILM_SHADOW_ENABLED=0")
destination.write_text("\n".join(out) + "\n", encoding="utf-8")
PY
  crontab "$RELEASE_DIR/crontab.next"
fi
phase_end

phase_start "compose config"
"${compose[@]}" config --quiet
phase_end

phase_start "drain jobs"
echo "Waiting for active app jobs to finish before deployment."
if "${compose[@]}" exec -T app true >/dev/null 2>&1; then
  deadline=$((SECONDS + DRAIN_TIMEOUT))
  until "${compose[@]}" exec -T app python - <<'PY'
from pathlib import Path

lock = Path("/runtime-control/.runtime.lock")
target = lock.stat()
holders = []
for proc in Path("/proc").iterdir():
    if not proc.name.isdigit() or proc.name == "1":
        continue
    try:
        for fd in (proc / "fd").iterdir():
            try:
                st = fd.stat()
            except FileNotFoundError:
                continue
            if (st.st_dev, st.st_ino) == (target.st_dev, target.st_ino):
                cmdline = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()
                holders.append(f"pid={proc.name} fd={fd.name} {cmdline}")
    except Exception:
        pass
if holders:
    print("Runtime worker leases still active:")
    print("\n".join(holders))
    raise SystemExit(75)
PY
  do
    [[ "$SECONDS" -lt "$deadline" ]] || {
      echo "Timed out waiting for active Project Paper jobs to finish. No container replacement attempted." >&2
      exit 75
    }
    echo "Runtime worker lease is busy; waiting before deployment."
    sleep 10
  done
else
  echo "Existing app container is unavailable; skipping in-container runtime lease drain." \
    | tee "$RELEASE_DIR/drain-skipped.log"
fi
phase_end

phase_start "backup database"
if ! "${compose[@]}" exec -T app bash scripts/backup_db.sh /app/data/paper_agent.db /backups \
  > "$RELEASE_DIR/pre-update-db-backup.log" 2>&1; then
  echo "Existing app container backup command unavailable; using one-shot image backup." \
    | tee -a "$RELEASE_DIR/pre-update-db-backup.log"
  docker run --rm --volumes-from paper-mini-production-app-1 --user 10001:10001 \
    --entrypoint bash "$APP_IMAGE" scripts/backup_db.sh /app/data/paper_agent.db /backups \
    >> "$RELEASE_DIR/pre-update-db-backup.log" 2>&1
fi
phase_end

phase_start "stop app"
"${compose[@]}" stop app
APP_REPLACEMENT_STARTED=1
phase_end

phase_start "verify exclusive runtime"
docker run --rm --volumes-from paper-mini-production-app-1 --user 10001:10001 --entrypoint python "$APP_IMAGE" - <<'PY'
from pathlib import Path
from paper_agents.migration_lifecycle import lease

with lease(Path("/runtime-control"), exclusive=True):
    pass
PY
phase_end

phase_start "recreate app"
"${compose[@]}" up -d --no-deps --pull never --force-recreate app
phase_end

phase_start "readiness"
deadline=$((SECONDS + 120))
until "${compose[@]}" exec -T app python -m paper_agents.package_runtime check-app >/dev/null 2>&1; do
  [[ "$SECONDS" -lt "$deadline" ]] || {
    "${compose[@]}" logs --no-color --tail=200 app > "$RELEASE_DIR/app-readiness-failure.log" 2>&1 || true
    echo "App readiness failed. Evidence: $RELEASE_DIR" >&2
    exit 1
  }
  sleep 3
done
phase_end

phase_start "post deploy checks"
curl --fail --silent --show-error http://127.0.0.1:8000/ > "$RELEASE_DIR/home.html"
"${compose[@]}" exec -T app python -m paper_agents.package_runtime check-model \
  > "$RELEASE_DIR/qwen-check.json"
"${compose[@]}" exec -T app python - <<'PY' > "$RELEASE_DIR/minilm-check.json"
import json
from paper_agents.curator_interest import default_interest_scorer, score_interest_fit

result = score_interest_fit(
    {
        "title": "Production incident diagnosis with operational telemetry",
        "abstract": (
            "This verification abstract describes incident diagnosis using production logs, "
            "metrics, traces, and service dependencies in a realistic microservice environment."
        ),
    },
    ["AIOps incident diagnosis using logs metrics traces and system relationships"],
    default_interest_scorer(),
)
print(json.dumps(result, sort_keys=True))
if result.get("status") != "scored":
    raise SystemExit("MiniLM Curator interest-fit verification failed")
PY
if ! "${compose[@]}" exec -T app python -m paper_agents.cli zenml-pilot --db /app/data/paper_agent.db --fetch-limit "${PAPER_ZENML_PILOT_FETCH:-200}" --keep "${PAPER_ZENML_PILOT_KEEP:-3}" \
  > "$RELEASE_DIR/zenml-pilot-import.txt" 2>&1; then
  echo "ZenML pilot import failed; deployment remains healthy. Evidence: $RELEASE_DIR/zenml-pilot-import.txt" >&2
  cat "$RELEASE_DIR/zenml-pilot-import.txt" >&2 || true
fi
"${compose[@]}" ps > "$RELEASE_DIR/after-ps.txt"
docker inspect paper-mini-production-app-1 > "$RELEASE_DIR/after-app-inspect.json" 2>/dev/null || true

verification_revision=$(docker inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' paper-mini-production-app-1)
verification_status=$(docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' paper-mini-production-app-1)
verification_image=$(docker inspect --format '{{.Config.Image}}' paper-mini-production-app-1)
verification_arxiv_progress=$("${compose[@]}" exec -T app sh -lc 'printf %s "${PAPER_ARXIV_PROGRESS:-unset}"')
verification_openalex_cursor=$("${compose[@]}" exec -T app sh -lc 'printf %s "${PAPER_OPENALEX_CURSOR:-unset}"')
verification_semantic_scholar_progress=$("${compose[@]}" exec -T app sh -lc 'printf %s "${PAPER_SEMANTIC_SCHOLAR_PROGRESS:-unset}"')
verification_minilm_enabled=$("${compose[@]}" exec -T app sh -lc 'printf %s "${PAPER_AGENT_MINILM_ENABLED:-unset}"')
verification_curator_model=$("${compose[@]}" exec -T app sh -lc 'printf %s "${PAPER_AGENT_CURATOR_MODEL:-unset}"')
verification_minilm_runner=$(sha256sum "$CANDIDATE_DIR/scripts/minilm_eval_after_pipeline.sh" | awk '{print $1}')
expected_minilm_runner=$(sha256sum "$SOURCE_DIR/scripts/minilm_eval_after_pipeline.sh" | awk '{print $1}')
verification_minilm_shadow_runner=$(sha256sum "$CANDIDATE_DIR/scripts/minilm_shadow_after_pipeline.sh" | awk '{print $1}')
expected_minilm_shadow_runner=$(sha256sum "$SOURCE_DIR/scripts/minilm_shadow_after_pipeline.sh" | awk '{print $1}')
verification_minilm_cron=$(grep -c '^PAPER_MINILM_EVAL_ENABLED=0$' "$RELEASE_DIR/crontab.next")
verification_minilm_shadow_cron=$(grep -c '^PAPER_MINILM_SHADOW_ENABLED=0$' "$RELEASE_DIR/crontab.next")
image_size_mib=$(sed -n 's/^image_size_mib=//p' "$RELEASE_DIR/image-metadata.env")
image_layer_count=$(sed -n 's/^image_layer_count=//p' "$RELEASE_DIR/image-metadata.env")
previous_image_layer_count=$(sed -n 's/^previous_image_layer_count=//p' "$RELEASE_DIR/image-metadata.env")
reused_image_layer_count=$(sed -n 's/^reused_image_layer_count=//p' "$RELEASE_DIR/image-metadata.env")
new_image_layer_count=$(sed -n 's/^new_image_layer_count=//p' "$RELEASE_DIR/image-metadata.env")
removed_image_layer_count=$(sed -n 's/^removed_image_layer_count=//p' "$RELEASE_DIR/image-metadata.env")
{
  printf 'revision=%s\n' "$verification_revision"
  printf 'status=%s\n' "$verification_status"
  printf 'PAPER_ARXIV_PROGRESS=%s\n' "$verification_arxiv_progress"
  printf 'PAPER_OPENALEX_CURSOR=%s\n' "$verification_openalex_cursor"
  printf 'PAPER_SEMANTIC_SCHOLAR_PROGRESS=%s\n' "$verification_semantic_scholar_progress"
  printf 'PAPER_AGENT_MINILM_ENABLED=%s\n' "$verification_minilm_enabled"
  printf 'PAPER_AGENT_CURATOR_MODEL=%s\n' "$verification_curator_model"
  printf 'image=%s\n' "$verification_image"
  printf 'image_size_mib=%s\n' "$image_size_mib"
  printf 'image_layer_count=%s\n' "$image_layer_count"
  printf 'previous_image_layer_count=%s\n' "$previous_image_layer_count"
  printf 'reused_image_layer_count=%s\n' "$reused_image_layer_count"
  printf 'new_image_layer_count=%s\n' "$new_image_layer_count"
  printf 'removed_image_layer_count=%s\n' "$removed_image_layer_count"
  printf 'minilm_runner_sha256=%s\n' "$verification_minilm_runner"
  printf 'minilm_shadow_runner_sha256=%s\n' "$verification_minilm_shadow_runner"
  printf 'PAPER_MINILM_EVAL_ENABLED=%s\n' "$([[ "$verification_minilm_cron" == 1 ]] && printf 0 || printf invalid)"
  printf 'PAPER_MINILM_SHADOW_ENABLED=%s\n' "$([[ "$verification_minilm_shadow_cron" == 1 ]] && printf 0 || printf invalid)"
} | tee "$RELEASE_DIR/post-deploy-verification.txt"
if [[ -n "$ARXIV_PROGRESS" && "$verification_arxiv_progress" != "$ARXIV_PROGRESS" ]]; then
  echo "Post-deploy verification failed: PAPER_ARXIV_PROGRESS=$verification_arxiv_progress, expected $ARXIV_PROGRESS" >&2
  exit 1
fi
if [[ -n "$OPENALEX_CURSOR" && "$verification_openalex_cursor" != "$OPENALEX_CURSOR" ]]; then
  echo "Post-deploy verification failed: PAPER_OPENALEX_CURSOR=$verification_openalex_cursor, expected $OPENALEX_CURSOR" >&2
  exit 1
fi
if [[ -n "$SEMANTIC_SCHOLAR_PROGRESS" && "$verification_semantic_scholar_progress" != "$SEMANTIC_SCHOLAR_PROGRESS" ]]; then
  echo "Post-deploy verification failed: PAPER_SEMANTIC_SCHOLAR_PROGRESS=$verification_semantic_scholar_progress, expected $SEMANTIC_SCHOLAR_PROGRESS" >&2
  exit 1
fi
if [[ "$verification_minilm_enabled" != 1 ]]; then
  echo "Post-deploy verification failed: PAPER_AGENT_MINILM_ENABLED=$verification_minilm_enabled, expected 1" >&2
  exit 1
fi
if [[ "$verification_curator_model" != qwen3:4b ]]; then
  echo "Post-deploy verification failed: PAPER_AGENT_CURATOR_MODEL=$verification_curator_model, expected qwen3:4b" >&2
  exit 1
fi
if [[ "$verification_minilm_runner" != "$expected_minilm_runner" ]]; then
  echo "Post-deploy verification failed: MiniLM evaluation runner mismatch" >&2
  exit 1
fi
if [[ "$verification_minilm_shadow_runner" != "$expected_minilm_shadow_runner" ]]; then
  echo "Post-deploy verification failed: MiniLM shadow runner mismatch" >&2
  exit 1
fi
if [[ "$verification_minilm_shadow_cron" != 1 ]]; then
  echo "Post-deploy verification failed: PAPER_MINILM_SHADOW_ENABLED=0 is missing or duplicated" >&2
  exit 1
fi
if [[ "$verification_minilm_cron" != 1 ]]; then
  echo "Post-deploy verification failed: PAPER_MINILM_EVAL_ENABLED=0 is missing or duplicated" >&2
  exit 1
fi
phase_end

mkdir -p "$(dirname "$DEPLOYED_MARKER")"
printf '%s\n' "$APP_IMAGE" > "$DEPLOYED_MARKER"

{
  printf 'completed_at=%s\n' "$(date -u +%FT%TZ)"
  printf 'result=pass\n'
  printf 'verified_revision=%s\n' "$verification_revision"
  printf 'verified_status=%s\n' "$verification_status"
  printf 'verified_arxiv_progress=%s\n' "$verification_arxiv_progress"
  printf 'verified_openalex_cursor=%s\n' "$verification_openalex_cursor"
  printf 'verified_semantic_scholar_progress=%s\n' "$verification_semantic_scholar_progress"
  printf 'verified_minilm_curator_enabled=%s\n' "$verification_minilm_enabled"
  printf 'verified_curator_model=%s\n' "$verification_curator_model"
  printf 'verified_image_size_mib=%s\n' "$image_size_mib"
  printf 'verified_image_layer_count=%s\n' "$image_layer_count"
  printf 'verified_previous_image_layer_count=%s\n' "$previous_image_layer_count"
  printf 'verified_reused_image_layer_count=%s\n' "$reused_image_layer_count"
  printf 'verified_new_image_layer_count=%s\n' "$new_image_layer_count"
  printf 'verified_removed_image_layer_count=%s\n' "$removed_image_layer_count"
  printf 'verified_minilm_runner_sha256=%s\n' "$verification_minilm_runner"
  printf 'verified_minilm_shadow_runner_sha256=%s\n' "$verification_minilm_shadow_runner"
  printf 'verified_minilm_eval_enabled=0\n'
  printf 'verified_minilm_shadow_enabled=%s\n' "$([[ "$verification_minilm_shadow_cron" == 1 ]] && printf 0 || printf invalid)"
  printf 'total_deploy_seconds=%s\n' "$(( $(date +%s) - DEPLOY_STARTED_EPOCH ))"
  printf 'rollback_script=%s\n' "$RELEASE_DIR/rollback.sh"
} >> "$RELEASE_DIR/update.env"
DEPLOYMENT_SUCCESS=1

echo "Mini production app update complete."
echo "Evidence: $RELEASE_DIR"
echo "Rollback script: $RELEASE_DIR/rollback.sh"
if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
  {
    echo "## Project Paper Mini deployment"
    echo
    echo "- Result: success"
    echo "- Image: \`$APP_IMAGE\`"
    echo "- Revision: \`$verification_revision\`"
    echo "- Status: \`$verification_status\`"
    echo "- Image size: \`${image_size_mib} MiB\`"
    echo "- Image layers: \`$image_layer_count\`"
    echo "- Layer diff: \`${reused_image_layer_count} reused / ${new_image_layer_count} new / ${removed_image_layer_count} removed\`"
    echo "- PAPER_ARXIV_PROGRESS: \`$verification_arxiv_progress\`"
    echo "- PAPER_OPENALEX_CURSOR: \`$verification_openalex_cursor\`"
    echo "- PAPER_SEMANTIC_SCHOLAR_PROGRESS: \`$verification_semantic_scholar_progress\`"
    echo "- PAPER_AGENT_MINILM_ENABLED: \`$verification_minilm_enabled\`"
    echo "- MiniLM Curator check: \`passed\`"
    echo "- Qwen3 Curator model: \`$verification_curator_model\`"
    echo "- Qwen3 Curator check: \`skipped during deploy\`"
    echo "- PAPER_MINILM_EVAL_ENABLED: \`0\`"
    echo "- PAPER_MINILM_SHADOW_ENABLED: \`0\`"
    echo "- MiniLM runner SHA256: \`$verification_minilm_runner\`"
    echo "- Evidence: \`$RELEASE_DIR\`"
    echo "- Rollback: \`$RELEASE_DIR/rollback.sh\`"
    echo
    echo "### Deploy phase timing"
    echo
    echo "| Phase | Seconds |"
    echo "| --- | ---: |"
    awk -F '\t' 'NR > 1 { printf "| `%s` | %s |\n", $1, $2 }' "$RELEASE_DIR/timing.tsv"
    echo
    echo "- Total deploy script time: \`$(( $(date +%s) - DEPLOY_STARTED_EPOCH ))s\`"
  } >> "$GITHUB_STEP_SUMMARY"
fi
