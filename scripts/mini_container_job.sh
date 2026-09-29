#!/usr/bin/env bash
# Host cron retains scheduling; execute the existing wrapper inside the running app.
set -euo pipefail
case "${1:-}" in
  arxiv|openalex|semantic|core|profile|backup) ;;
  *) echo 'Expected arxiv, openalex, semantic, core, profile, or backup' >&2; exit 64 ;;
esac
if [[ $# -ne 1 ]]; then echo 'Exactly one job name required' >&2; exit 64; fi
: "${PAPER_MIGRATION_ENV_FILE:?Set absolute private migration env-file path}"
case "$PAPER_MIGRATION_ENV_FILE" in /*) ;; *) echo 'Absolute env-file path required' >&2; exit 64 ;; esac
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
deploy_lock_file="${PAPER_MIGRATION_DEPLOY_LOCK_FILE:-}"
if [[ -n "$deploy_lock_file" ]]; then
  case "$deploy_lock_file" in /*) ;; *) echo 'Deploy lock file must be absolute' >&2; exit 64 ;; esac
  mkdir -p "$(dirname "$deploy_lock_file")"
  exec 8>"$deploy_lock_file"
  if ! flock -n -s 8; then
    echo 'Skipping Project Paper job: deployment is in progress.'
    exit 0
  fi
fi
compose=(docker compose --env-file "$PAPER_MIGRATION_ENV_FILE" -f "$repo/docker-compose.mini-migration.yml")
if [[ -n "${PAPER_MIGRATION_EXTRA_COMPOSE_FILES:-}" ]]; then
  IFS=':' read -r -a extra_compose_files <<< "$PAPER_MIGRATION_EXTRA_COMPOSE_FILES"
  for compose_file in "${extra_compose_files[@]}"; do
    [[ -n "$compose_file" ]] || continue
    case "$compose_file" in /*) ;; *) echo 'Extra compose files must be absolute paths' >&2; exit 64 ;; esac
    compose+=(-f "$compose_file")
  done
fi
# Never source an env file or interpolate its values as shell commands.
job_core_source="${PAPER_CORE_SOURCE:-0}"
if [[ "$1" == core ]]; then
  job_core_source=1
fi
"${compose[@]}" exec -T app \
  env PAPER_AGENT_REPO=/app PAPER_AGENT_LOCK_DIR=/runtime-control \
  PAPER_AGENT_SELF_UPDATE=0 PAPER_AGENT_TOPIC_SLOT=0 \
  PAPER_AGENT_PROFILE_REBUILD_ANCHOR=2026-09-07 TZ=America/Los_Angeles \
  PAPER_CORE_SOURCE="$job_core_source" \
  python -m paper_agents.migration_job "$1"

if [[ "${PAPER_MINILM_EVAL_ENABLED:-0}" == "1" ]]; then
  case "$1" in
    arxiv) eval_source=arxiv ;;
    openalex) eval_source=openalex ;;
    semantic) eval_source=semantic_scholar ;;
    *) exit 0 ;;
  esac
  if ! app_container=$("${compose[@]}" ps -q app); then
    echo "MiniLM Eval skipped after successful $1 pipeline: app container lookup failed." >&2
  elif [[ -z "$app_container" ]]; then
    echo "MiniLM Eval skipped after successful $1 pipeline: app container was not found." >&2
  elif ! bash "$repo/scripts/minilm_eval_after_pipeline.sh" "$eval_source" "$app_container" "$repo"; then
    echo "MiniLM Eval failed after successful $1 pipeline; production results are unchanged." >&2
  fi
fi
