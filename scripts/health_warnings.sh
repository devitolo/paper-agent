#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${PAPER_AGENT_REPO:-$HOME/workspace/paper-agent}"
DB_PATH="${PAPER_AGENT_DB:-data/paper_agent.db}"
DAYS="${PAPER_AGENT_HEALTH_DAYS:-21}"

cd "$REPO_DIR"

echo "== Project Paper health warnings =="
python3 -m paper_agents.cli db health --db "$DB_PATH" --days "$DAYS"

if ! command -v sqlite3 >/dev/null 2>&1; then
  echo
  echo "sqlite3 is not available; skipping detailed warning rows."
  exit 0
fi

echo
echo "== Latest cycle recommendations missing triage summaries =="
sqlite3 -header -column "$DB_PATH" <<'SQL'
WITH latest_cycle AS (
  SELECT id
  FROM workflow_cycles
  ORDER BY id DESC
  LIMIT 1
), primary_source AS (
  SELECT paper_id, source, source_id, url, pdf_url
  FROM paper_sources
  WHERE id IN (SELECT MIN(id) FROM paper_sources GROUP BY paper_id)
)
SELECT
  recommendations.id AS recommendation_id,
  papers.id AS paper_id,
  primary_source.source,
  papers.title,
  primary_source.pdf_url
FROM recommendations
JOIN curator_runs ON curator_runs.id = recommendations.curator_run_id
JOIN latest_cycle ON latest_cycle.id = curator_runs.workflow_cycle_id
JOIN papers ON papers.id = recommendations.paper_id
LEFT JOIN primary_source ON primary_source.paper_id = papers.id
LEFT JOIN artifacts triage
  ON triage.paper_id = papers.id
 AND triage.artifact_type = 'triage_summary'
WHERE triage.id IS NULL
ORDER BY recommendations.recommendation_order ASC;
SQL

echo
echo "== Recent unresolved failed profile apply attempts =="
sqlite3 -header -column "$DB_PATH" <<'SQL'
SELECT
  failed_attempts.id,
  failed_attempts.created_at,
  failed_attempts.provider,
  failed_attempts.model,
  failed_attempts.status,
  substr(failed_attempts.error, 1, 160) AS error
FROM feedback_profile_apply_attempts failed_attempts
WHERE failed_attempts.status = 'failed'
  AND failed_attempts.created_at >= datetime('now', '-7 days')
  AND NOT EXISTS (
    SELECT 1
    FROM feedback_profile_apply_attempts successful_attempts
    WHERE successful_attempts.dry_run = 0
      AND successful_attempts.status = 'succeeded'
      AND successful_attempts.id > failed_attempts.id
  )
ORDER BY id DESC
LIMIT 10;
SQL

echo
echo "== Structured feedback waiting for profile apply =="
sqlite3 -header -column "$DB_PATH" <<'SQL'
SELECT
  structured_feedback.id,
  structured_feedback.paper_id,
  structured_feedback.decision,
  structured_feedback.score,
  structured_feedback.created_at
FROM structured_feedback
LEFT JOIN feedback_profile_applications
  ON feedback_profile_applications.structured_feedback_id = structured_feedback.id
WHERE feedback_profile_applications.id IS NULL
ORDER BY structured_feedback.id ASC
LIMIT 20;
SQL

cat <<'TEXT'

Common repair commands:
  scripts/fix_missing_triage_summaries.sh --quick --limit 5
  scripts/apply_pending_feedback_profile.sh --dry-run
  scripts/apply_pending_feedback_profile.sh --apply
TEXT
