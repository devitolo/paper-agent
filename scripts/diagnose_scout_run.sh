#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${PAPER_AGENT_REPO:-$HOME/workspace/paper-agent}"
DB_PATH="${PAPER_AGENT_DB:-data/paper_agent.db}"
SOURCE="${1:-}"

if [[ -z "$SOURCE" ]]; then
  echo "Usage: scripts/diagnose_scout_run.sh <arxiv|openalex|semantic_scholar>" >&2
  exit 2
fi

case "$SOURCE" in
  arxiv|openalex|semantic_scholar) ;;
  *)
    echo "Unknown Scout source: $SOURCE" >&2
    exit 2
    ;;
esac

if ! command -v sqlite3 >/dev/null 2>&1; then
  echo "sqlite3 is required to diagnose Scout runs." >&2
  exit 1
fi

cd "$REPO_DIR"

echo "== Latest $SOURCE Scout run =="
sqlite3 -header -column "$DB_PATH" <<SQL
SELECT
  scout_runs.id,
  scout_runs.workflow_cycle_id AS cycle,
  scout_runs.attempt_number AS attempt,
  scout_runs.started_at,
  scout_runs.completed_at,
  scout_runs.target_candidates AS target,
  scout_runs.max_candidates AS fetch_limit,
  scout_runs.freshness_months,
  COUNT(scout_candidates.id) AS candidates,
  SUM(CASE WHEN scout_candidates.excluded = 0 THEN 1 ELSE 0 END) AS eligible,
  SUM(CASE WHEN scout_candidates.excluded = 1 THEN 1 ELSE 0 END) AS excluded
FROM scout_runs
LEFT JOIN scout_candidates ON scout_candidates.scout_run_id = scout_runs.id
WHERE scout_runs.source = '$SOURCE'
GROUP BY scout_runs.id
ORDER BY scout_runs.id DESC
LIMIT 1;
SQL

echo
echo "== Run inputs and source diagnostics =="
sqlite3 -header -column "$DB_PATH" <<SQL
SELECT
  id,
  topics_json AS topics,
  diagnostics_json AS diagnostics,
  warnings_json AS warnings,
  errors_json AS errors
FROM scout_runs
WHERE source = '$SOURCE'
ORDER BY id DESC
LIMIT 1;
SQL

echo
echo "== Exclusion reasons in latest run =="
sqlite3 -header -column "$DB_PATH" <<SQL
WITH latest_run AS (
  SELECT id
  FROM scout_runs
  WHERE source = '$SOURCE'
  ORDER BY id DESC
  LIMIT 1
)
SELECT
  COALESCE(exclusion_reason, 'unspecified') AS reason,
  COUNT(*) AS count
FROM scout_candidates
WHERE scout_run_id = (SELECT id FROM latest_run)
  AND excluded = 1
GROUP BY reason
ORDER BY count DESC, reason;
SQL

echo
echo "== Candidates in latest run =="
sqlite3 -header -column "$DB_PATH" <<SQL
WITH latest_run AS (
  SELECT id
  FROM scout_runs
  WHERE source = '$SOURCE'
  ORDER BY id DESC
  LIMIT 1
)
SELECT
  scout_candidates.id AS candidate_id,
  papers.id AS paper_id,
  scout_candidates.excluded,
  scout_candidates.exclusion_reason,
  papers.title,
  scout_candidates.source_query
FROM scout_candidates
JOIN papers ON papers.id = scout_candidates.paper_id
WHERE scout_candidates.scout_run_id = (SELECT id FROM latest_run)
ORDER BY scout_candidates.excluded ASC, scout_candidates.retrieval_order ASC
LIMIT 30;
SQL
