#!/usr/bin/env bash
set -euo pipefail

# Read-only production health snapshot for the self-hosted Mini runner.
# The production env file is never sourced or printed; Compose reads it only
# for interpolation while exec'ing into the already-running app container.
REPO_DIR="${PAPER_AGENT_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ENV_FILE="${PAPER_MINI_ENV_FILE:-${PAPER_MIGRATION_ENV_FILE:-}}"
COMPOSE_FILE="${PAPER_MINI_COMPOSE_FILE:-$REPO_DIR/docker-compose.mini-migration.yml}"
DAYS="${PAPER_AGENT_HEALTH_DAYS:-21}"
OUTPUT_FILE="${PAPER_MINI_HEALTH_OUTPUT:-}"

if [[ -z "$ENV_FILE" || ! -f "$ENV_FILE" ]]; then
  echo "Missing production env file. Set PAPER_MINI_ENV_FILE to its absolute path." >&2
  exit 2
fi
case "$ENV_FILE" in /*) ;; *) echo "PAPER_MINI_ENV_FILE must be an absolute path." >&2; exit 2 ;; esac
case "$COMPOSE_FILE" in /*) ;; *) echo "PAPER_MINI_COMPOSE_FILE must be an absolute path." >&2; exit 2 ;; esac

tmp_json="$(mktemp)"
trap 'rm -f "$tmp_json"' EXIT

compose=(docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE")
if ! "${compose[@]}" exec -T app python -m paper_agents.cli db health --days "$DAYS" --json >"$tmp_json"; then
  echo "CRITICAL: unable to collect health from the running production app container." >&2
  exit 2
fi

if [[ -n "$OUTPUT_FILE" ]]; then
  mkdir -p "$(dirname "$OUTPUT_FILE")"
  cp "$tmp_json" "$OUTPUT_FILE"
fi

python3 - "$tmp_json" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
warnings = payload.get("warnings") or []
profile = payload.get("profile_maintenance") or {}
status = "critical" if any(w.get("level") == "critical" for w in warnings) else "warning" if warnings else "healthy"
latest = payload.get("latest_cycle") or {}
print(f"status={status}")
print(f"latest_cycle_id={latest.get('id', '')} latest_cycle_state={latest.get('state', '')} days_since_last_cycle={payload.get('days_since_last_cycle', '')}")
for warning in warnings:
    print(f"{warning.get('level', 'warning').upper()}: {warning.get('message', '')}")
if isinstance(profile, dict):
    for key, value in profile.items():
        if value not in (None, 0, [], {}, ""):
            print(f"profile_{key}={value}")
elif profile:
    # The health payload currently returns maintenance notices as a list.
    # Keep the snapshot readable without assuming a mapping shape.
    print("profile_maintenance=" + json.dumps(profile, sort_keys=True))
print("latest_source_runs:")
for run in payload.get("latest_scout_runs_by_source") or []:
    print("  " + json.dumps(run, sort_keys=True))
print("funnel:")
for row in (payload.get("source_breakdown") or {}).get("funnel") or []:
    print("  " + json.dumps(row, sort_keys=True))
print("recent_cycle_recommendations=" + json.dumps(payload.get("recent_cycle_recommendations") or [], sort_keys=True))
print("recent_job_events:")
for event in (payload.get("job_events") or [])[:10]:
    print("  " + json.dumps(event, sort_keys=True))
if status != "healthy":
    raise SystemExit(1)
PY
