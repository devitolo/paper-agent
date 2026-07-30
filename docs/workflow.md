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

Feedback Agent is the next major product flow. It will receive the manually copied final ChatGPT discussion summary, store the exact raw summary immutably, create parse attempts, store structured feedback, and create a new profile version with provenance.

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
8. Recommended PDFs are downloaded when available.
9. Local Ollama extraction creates triage summaries for recommended PDFs.
10. The workflow waits for manual ChatGPT discussion.
11. Later, the Feedback Agent ingests the final discussion summary and updates profile history.

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

`--keep` is capped at three recommendations. `--max-scout-attempts` controls the bounded re-scout loop.

## Review Queue UI

```bash
python3 -m paper_agents.cli web --host 127.0.0.1 --port 8000
```

The UI lists Curator recommendations from SQLite, displays local triage summary fields when available, opens registered artifacts, exposes the original paper link, supports status filters, latest/score sorting, full/condensed views, and appends lightweight status rows.

## Nightly Cron

The current Mac mini setup uses cron at 5:00 AM local time:

```bash
(crontab -l 2>/dev/null; echo "0 5 * * * cd $HOME/workspace/paper-agent && mkdir -p logs && scripts/nightly_pipeline.sh >> logs/pipeline-daily.log 2>&1") | crontab -
```

systemd timers remain the preferred later option once logging and failure recovery are more mature.
