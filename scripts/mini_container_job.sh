#!/usr/bin/env bash
# Host cron retains scheduling; execute the existing wrapper inside the running app.
set -euo pipefail
case "${1:-}" in
  arxiv|openalex|semantic|profile|backup) ;;
  *) echo 'Expected arxiv, openalex, semantic, profile, or backup' >&2; exit 64 ;;
esac
if [[ $# -ne 1 ]]; then echo 'Exactly one job name required' >&2; exit 64; fi
: "${PAPER_MIGRATION_ENV_FILE:?Set absolute private migration env-file path}"
case "$PAPER_MIGRATION_ENV_FILE" in /*) ;; *) echo 'Absolute env-file path required' >&2; exit 64 ;; esac
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
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
exec "${compose[@]}" exec -T app \
  env PAPER_AGENT_REPO=/app PAPER_AGENT_LOCK_DIR=/runtime-control \
  PAPER_AGENT_SELF_UPDATE=0 PAPER_AGENT_TOPIC_SLOT=0 \
  PAPER_AGENT_PROFILE_REBUILD_ANCHOR=2026-09-07 TZ=America/Los_Angeles \
  python -m paper_agents.migration_job "$1"
