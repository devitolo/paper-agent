#!/usr/bin/env bash
# Execute against the same app image, DB/config volumes and lifecycle as pipeline jobs.
set -euo pipefail
[[ "${PAPER_AGENT_SYNC_ENABLED:-0}" == 1 ]] || { echo 'Mini AWS sync disabled'; exit 0; }
[[ "${PAPER_AGENT_SYNC_EXPLICIT_PRODUCTION:-0}" == 1 ]] || { echo 'Explicit sync activation required' >&2; exit 64; }
case "${1:-}" in pull|push|retry|recover|publish-health) ;; *) echo 'Usage: mini_sync_exchange.sh pull|push|retry|recover|publish-health' >&2; exit 64 ;; esac
: "${PAPER_MIGRATION_ENV_FILE:?Set private migration env-file path}"
: "${PAPER_AGENT_SYNC_AWS_URL:?Set HTTPS AWS origin}"
case "$PAPER_MIGRATION_ENV_FILE" in /*) ;; *) exit 64 ;; esac
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose=(docker compose --env-file "$PAPER_MIGRATION_ENV_FILE" -f "$repo/docker-compose.mini-migration.yml")
if [[ -n "${PAPER_MIGRATION_EXTRA_COMPOSE_FILES:-}" ]]; then
  IFS=':' read -r -a extra_files <<< "$PAPER_MIGRATION_EXTRA_COMPOSE_FILES"
  for file in "${extra_files[@]}"; do
    case "$file" in /*) compose+=(-f "$file") ;; *) exit 64 ;; esac
  done
fi
# Private env-file credentials must already be provisioned inside app; never echo
# or put the HMAC secret in host process arguments.
"${compose[@]}" exec -T app env PAPER_AGENT_SYNC_ENABLED=1 \
  PAPER_AGENT_SYNC_EXPLICIT_PRODUCTION=1 \
  PAPER_AGENT_ROUTINE_SYNC="${PAPER_AGENT_ROUTINE_SYNC:-0}" \
  PAPER_AGENT_SYNC_PRODUCER="${PAPER_AGENT_SYNC_PRODUCER:-mini-production}" \
  PAPER_AGENT_SYNC_ENVIRONMENT="${PAPER_AGENT_SYNC_ENVIRONMENT:-production}" \
  python -m paper_agents.whole_cycle_exchange "$1" \
  --db-path /app/data/paper_agent.db --root /app \
  --state-dir /app/data/sync-state --aws-url "$PAPER_AGENT_SYNC_AWS_URL"
