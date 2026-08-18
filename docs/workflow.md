# Workflow

This document describes the Project Paper MVP workflow after the V2 backend reset.

```text
Scout
  -> Curator
  -> manual ChatGPT Paper Discussion
  -> manual final discussion summary handoff
  -> Feedback Agent
  -> SQLite
```

## Role Boundaries

Scout retrieves configured sources, normalizes candidate records, deduplicates source results, marks previously discovered papers as excluded, records source/query telemetry, and writes candidate pools. Scout does not score, rank, recommend, or persist preference scores.

Curator reads the Scout candidate pool, the active profile version, historical state, and active guidance. It evaluates every eligible candidate, stores scores and rationales, recommends at most three papers, and writes active guidance for later Scout runs. Re-scout requests are bounded by the workflow cycle's maximum Scout attempt count.

Feedback Agent uses a blob-first product path: the user pastes a final ChatGPT discussion summary into Project Paper, and the shared CLI/UI ingestion backend stores the exact raw blob immutably, creates parse attempts, stores deterministic v1 structured feedback, and runs Gemini incremental profile apply after Review Queue submit. Manual CLI dry-run/apply remains available for testing and operations.

## Manual MVP Boundaries

These remain manual by design:

1. The user copies a recommended paper/link into the ChatGPT Paper Discussion conversation.
2. The user copies ChatGPT's final discussion summary back into Project Paper for the Feedback Agent.

Do not automate these handoffs until the manual loop is clearly useful.

## Scheduled Run

1. A cron job or future systemd timer starts `pipeline-daily`.
2. A `workflow_cycles` row is created.
3. The active profile version is loaded or seeded from `data/profile.json`.
4. Scout loads active scouting guidance, fetches arXiv candidates, deduplicates them, and records all candidates for the run.
5. Previously discovered papers are recorded as excluded Scout candidates with an exclusion reason.
6. Curator evaluates every eligible candidate.
7. Curator writes up to three recommendations and active guidance for future Scout runs.
8. Reviewer downloads recommended PDFs when available.
9. Reviewer runs local Ollama extraction and stores triage summary artifacts for recommended PDFs.
10. The workflow waits for manual ChatGPT discussion.
11. The Feedback Agent ingests the final discussion summary into raw and structured feedback tables.
12. Review Queue feedback submit automatically runs the Gemini incremental profile update after storing raw and structured feedback.
13. For testing or operations, the operator can still run `feedback apply --dry-run`, review Gemini's proposed profile update, then run `feedback apply` manually.

## SQLite Registry

The V2 registry stores:

- `papers`: canonical paper identity, DOI/arXiv IDs, metadata, and discovery timestamps.
- `paper_sources`: alternate source records and URLs for the same canonical paper.
- `workflow_cycles`: explicit stage/state for a recommendation cycle.
- `scout_runs`: source attempts, topics, guidance, and telemetry.
- `scout_candidates`: retrieval order, new/known status, exclusion status, and source diagnostics. No preference scores live here.
- `curator_runs`: scoring/recommendation runs tied to workflow cycles and profile versions.
- `curator_evaluations`: scores and rationales for every candidate considered.
- `recommendations`: up to three ordered recommendations per Curator run.
- `scouting_guidance`: append-only Curator guidance with one active version.
- `raw_feedback`: immutable manually submitted discussion summaries, deduped by content hash.
- `feedback_parse_attempts`: repeatable parse attempts over raw feedback.
- `structured_feedback`: parsed decisions, observations, scores, and preference signals.
- `feedback_profile_applications`: applied-feedback tracking that links consumed structured feedback rows to generated profile versions.
- `feedback_profile_apply_attempts`: durable success/failure records for manual apply, auto-apply, and rebuild attempts.
- `profile_versions`: append-only long-term preference profile versions.
- `artifacts`: PDFs, triage summaries, and generated reviews.
- `feedback`: lightweight UI status rows for the review queue.

## Reset And Bootstrap

The V2 reset is intentionally destructive because early MVP data is disposable:

```bash
python3 -m paper_agents.cli db reset --yes
python3 -m paper_agents.cli bootstrap known-papers
```

The bootstrap command inserts known seed papers as manual backfill recommendations without inventing official feedback.

## Inspection Commands

```bash
python3 -m paper_agents.cli db stats
python3 -m paper_agents.cli db recent-runs --limit 5
python3 -m paper_agents.cli db papers --selected --limit 10
```

## Repository Path

Use `/Users/vhl/workspace/paper-agent` as the canonical local checkout path for Project Paper commands, cron entries, deployment scripts, and Codex follow-up work.

## Pipeline Commands

Quick interactive run:

```bash
python3 -m paper_agents.cli pipeline-daily   --quick   --fetch 20   --keep 3   --request-delay 5   --retries 4   --source-timeout 90
```

Full scheduled run:

```bash
python3 -m paper_agents.cli pipeline-daily --fetch 20 --keep 3
```

`--keep` is capped at three recommendations. `--max-scout-attempts` controls the bounded re-scout loop. arXiv remains the default Scout source; Semantic Scholar can be selected with `--source semantic_scholar`, and OpenAlex can be selected with `--source openalex`, for `scout-daily` or `pipeline-daily`. Semantic Scholar may optionally use `SEMANTIC_SCHOLAR_API_KEY`; OpenAlex uses its public API without a key. Non-arXiv sources are opt-in and are not part of the nightly cron default yet. Default Scout topics are intentionally broad across AIOps, LLM/agentic operations, incident response, root-cause/failure diagnosis, observability/log/trace analysis, debugging, software maintenance, SRE, cloud operations, and production engineering. Curator penalizes obvious physical-world incident domains such as railway, traffic/vehicular, medical/healthcare, grid, and transportation incidents.

## Review Queue UI

```bash
python3 -m paper_agents.cli web --host 127.0.0.1 --port 8000
```

The UI lists Curator recommendations from SQLite, displays local triage summary fields when available, opens registered artifacts, exposes the original paper link with a URL copy control, and appends lightweight status rows. Non-empty Feedback boxes are also stored as exact `raw_feedback` blobs, parsed by deterministic parser v1 into `feedback_parse_attempts` and `structured_feedback`, and intentionally do not update `profile_versions`, ScoutAgent, or CuratorAgent directly.

The same V2 storage path is available from the CLI:

```bash
python3 -m paper_agents.cli feedback add --paper-id 12 --status interested --file /tmp/feedback.txt
```

The Review Queue UI auto-applies the newly submitted structured feedback row to the active profile through Gemini after the feedback blob is safely stored. If Gemini/provider/JSON handling fails, the saved feedback is retained, the structured row remains unapplied, and the UI shows a warning banner. Inspect recent attempts:

```bash
sqlite3 data/paper_agent.db "SELECT id, provider, status, error, profile_version_id, created_at FROM feedback_profile_apply_attempts ORDER BY id DESC LIMIT 10;"
```

Manual profile apply remains available for recovery, testing, and operations:

```bash
python3 -m paper_agents.cli feedback apply --provider gemini --dry-run
python3 -m paper_agents.cli feedback apply --provider gemini
```

Full rebuild is a manual compression path that reads all structured feedback and creates a fresh compact profile. Do not run it automatically yet:

```bash
python3 -m paper_agents.cli feedback rebuild-profile --provider gemini --dry-run
python3 -m paper_agents.cli feedback rebuild-profile --provider gemini
```

Review profile quality after roughly 10 feedback items or if recommendation quality shows an obvious downward trend.


If older recommended papers have PDFs but no triage summaries, backfill those missing summaries without running Scout/Curator again:

```bash
python3 -m paper_agents.cli review-backfill --quick
```

## Nightly Cron

The current Mac mini setup uses cron at 5:00 AM local time:

```bash
(crontab -l 2>/dev/null; echo "0 5 * * * cd $HOME/workspace/paper-agent && mkdir -p logs && scripts/nightly_pipeline.sh >> logs/pipeline-daily.log 2>&1") | crontab -
```

systemd timers remain the preferred later option once logging and failure recovery are more mature.

## Backups And Logs

Back up SQLite weekly with the online backup API and verify the backup with `PRAGMA integrity_check`:

```bash
0 4 * * 0 cd $HOME/workspace/paper-agent && mkdir -p logs && scripts/backup_db.sh >> logs/backup-db.log 2>&1
```

Backups are written under `backups/` by default, named `paper_agent-YYYYmmdd-HHMMSS.db`, verified after creation, and intentionally ignored by git. For restore, copy a selected backup into place only after checking `PRAGMA integrity_check`; keep restore drills manual until operations mature.

Install weekly compressed log rotation for `logs/*.log`:

```bash
sudo cp deploy/project-paper.logrotate /etc/logrotate.d/project-paper
```

Tail current logs:

```bash
scripts/tail_logs.sh
```
