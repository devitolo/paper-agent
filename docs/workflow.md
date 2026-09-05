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

Scout retrieves configured sources, normalizes candidate records, expands source queries with deterministic feedback/profile guidance, deduplicates source results, marks previously discovered papers and clear feedback-avoid matches as excluded, records source/query telemetry, and writes candidate pools. Scout does not score, rank, recommend, call an LLM Scout agent, or perform deep paper/PDF reading.

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
4. Scout builds deterministic feedback/profile guidance, expands configured source queries with a small bounded set of boost terms, fetches candidates, deduplicates them, and records all candidates for the run.
5. Previously discovered papers and clear feedback-avoid matches are recorded as excluded Scout candidates with an exclusion reason.
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
- `scout_runs`: source attempts, topics, guidance IDs, diagnostics JSON, and telemetry.
- `scout_candidates`: retrieval order, new/known status, exclusion status, and source diagnostics such as feedback boost/avoid hits. No preference scores live here.
- `curator_runs`: scoring/recommendation runs tied to workflow cycles and profile versions.
- `curator_evaluations`: scores and rationales for every candidate considered.
- `recommendations`: up to three ordered recommendations per Curator run.
- `scouting_guidance`: append-only guidance records, including active Curator guidance and per-run Scout feedback/profile guidance snapshots.
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
python3 -m paper_agents.cli db health --days 21
python3 -m paper_agents.cli db health --days 21 --source openalex --json
```

`db health` computes operational rollups directly from raw SQLite facts, without materialized aggregate tables. It reports DB path/size/integrity, latest workflow cycle age/state, days since the last recommendation, source-aware Scout/Curator/Reviewer funnel counts, artifact gaps, unapplied structured feedback, recent profile apply failures, and warnings for stale cycles or unhealthy sources. Health warnings should represent actionable work rather than historical noise; the web page omits the warning section entirely when there are no warnings. Use `--days`, `--source`, and `--json` for range, source, and machine-readable output.

When `/health` shows warnings, run the focused Mini wrapper first:

```bash
scripts/health_warnings.sh
scripts/fix_missing_triage_summaries.sh --quick --limit 5
scripts/apply_pending_feedback_profile.sh --dry-run
scripts/apply_pending_feedback_profile.sh --apply
```

`scripts/health_warnings.sh` prints the normal health report plus the specific rows behind common warnings: latest-cycle recommendations with available PDFs but missing triage summaries, recent unresolved Gemini/profile apply failures, and structured feedback waiting for profile apply. `scripts/fix_missing_triage_summaries.sh --quick --limit 5` backfills summaries for recommendations with PDFs, or creates abstract-only triage summaries when no PDF can be downloaded but source metadata includes an abstract, without rerunning Scout or Curator. DOI-only Semantic Scholar/OpenAlex records without a PDF artifact do not create the missing-summary repair warning unless source metadata gives `review-backfill` enough abstract text to create an abstract-only triage artifact; they do not render an `Open PDF` action. `scripts/apply_pending_feedback_profile.sh --dry-run` previews pending profile apply; `--apply` writes the profile update, and a later successful non-dry-run profile apply clears the Gemini/profile failure warning. DB changes show up on browser refresh. Code, template, and static asset changes from `git pull` require restarting the long-running Python web server; static assets may also need a hard refresh if browser-cached.

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

arXiv remains the default Scout source and the daily cron source. Semantic Scholar can be selected with `--source semantic_scholar`, and OpenAlex can be selected with `--source openalex`, for `scout-daily` or `pipeline-daily`. When no explicit `--topic` is supplied, source jobs select an enabled topic from `config/topics.yaml`; explicit `--topic` values still override config for that one run. OpenAlex is available through a separate weekly rotating script rather than the daily arXiv path.

Scout also reads the active profile plus recent structured feedback to build deterministic guidance for every run. High-scored `keep` feedback can add a few boost terms to the source query set; low-scored `reject` feedback contributes avoid terms. The exact guidance is stored in `scouting_guidance` and copied into `scout_runs.diagnostics_json`. Candidate diagnostics record feedback boost/avoid hits, and only clear avoid-heavy matches with no positive hits are excluded before Curator. Softer matches remain visible for Curator evaluation. The sample size is still small, around 10 scored papers, so this path is deliberately conservative. Deep paper/PDF reading remains future Curator V2 evidence-aware reranking work, not Scout V2.

Semantic Scholar reads `SEMANTIC_SCHOLAR_API_KEY` and sends it as the `x-api-key` request header. The key is approved and a direct CLI test has succeeded, but the source remains opt-in and rate-limited. Approved key guidance is 1 request per second cumulatively across endpoints, so use `--request-delay 2` or higher. OpenAlex uses its public API without a key. Its adapter narrows source queries toward software/cloud/operations context, requests article-like work types, and filters obvious book/index/reference and biomedical noise before storage.

During bounded rescouts inside one workflow cycle, papers rediscovered earlier in the same cycle remain eligible instead of being marked `previously_discovered`; older-cycle discoveries are still excluded. Papers already recommended before the current Scout run are excluded as `already_recommended` so they do not consume new daily recommendation slots, including during later rescout attempts in the same workflow cycle. Each rescout attempt asks the source for a deeper candidate window, so a shallow stale result set does not repeat unchanged across attempts.

Scout topics are file-backed in `config/topics.yaml` and intentionally cover AIOps, LLM/agentic operations, incident response, root-cause/failure diagnosis, observability/log/trace analysis, debugging, software maintenance, SRE, cloud operations, and production engineering. Curator penalizes obvious physical-world incident domains such as railway, traffic/vehicular, medical/healthcare, grid, and transportation incidents. The web server includes `/topics`, the editable Topic Management V1 page. Source schedule inventory appears above All Topics, and All Topics is collapsed by default. The primary flow is TopicAgent: describe the scouting intent, local Ollama/Qwen (`qwen2.5:1.5b-instruct` by default) semantically infers one of `create_new`, `update_existing`, `remove_existing`, or `ask_clarifying_question`, then proposes label, query, sources, cadence, priority, and enabled state for approval before config is saved. Operators can tune this with `PAPER_AGENT_TOPIC_MODEL`, `PAPER_AGENT_TOPIC_OLLAMA_URL` or `PAPER_AGENT_OLLAMA_URL`, and `PAPER_AGENT_TOPIC_TIMEOUT_SECONDS` defaulting to 30 seconds. The proposal, previous conversation, and clarifying question loop stay in the same TopicAgent panel, and pending changes preview in the topic list before Apply. Invalid model JSON falls back to deterministic proposal logic for safe creates/duplicates/explicit actions; ambiguous change or remove intent falls back to a clarifying question instead of guessing. In product terms, `remove`, `delete`, and `drop` mean remove the topic from config; `disable`, `turn off`, and `pause` mean preserve the topic with `enabled: false`. Use row Enable/Disable actions for quick toggles, or Edit as the manual escape hatch for precise query, sources, cadence, priority, and enabled changes. Changes affect future scheduled runs only; `/topics` does not run Scout immediately.

## Review Queue UI

```bash
python3 -m paper_agents.cli web --host 127.0.0.1 --port 8000
```

The UI lists Curator recommendations from SQLite, displays local triage summary fields when available, keeps raw summary JSON out of regular cards, opens the original paper from the title, keeps Open PDF and URL copy next to source/date/source-id metadata, shows distinct compact source badges, filters by source when multiple Scout sources are present, and keeps the primary review workflow focused on selecting a paper and adding feedback. Header navigation and unlabeled filter controls sit in separate bands so the queue remains dense without crowding. The default sort is Highest score; Newest remains available, and Full/Condensed uses a compact two-state toggle. When PDF extraction is unavailable but source metadata includes an abstract, reviewer backfill creates an `Abstract-only triage` summary artifact from that abstract, marks the artifact metadata as abstract-only, and labels the card inside `Why this matches you` so users know the full PDF was not downloaded. If no triage summary artifact exists yet, the card can still show a clearly labeled `Source Abstract` fallback instead of pretending it has a full-paper summary. Recommended non-arXiv records remain visible even when no PDF is available, but DOI-only URLs are not rendered as `Open PDF`. Primary filters display as All papers, Scored, and Needs review; old lightweight status values such as interested, read_later, reviewed, and not_interested remain compatible with older records but no longer clutter the main filter list or primary card actions. Feedback is progressively disclosed behind Add feedback or View/edit feedback. Scored papers reopen View/edit feedback with the latest saved `raw_feedback.content` blob prefilled, falling back to legacy `feedback.notes` only for older records, so users can revise and resubmit the original Paper Discussion blob. `Copy discussion prompt` copies a handoff prompt for the separate Paper Discussion ChatGPT conversation using current card data, including source/PDF links, match score, optional user score, rationale, signals, and local context. Only saving a non-empty Feedback blob stores exact `raw_feedback`, parses deterministic v1 `structured_feedback`, and queues Gemini profile apply in the background so the browser can refresh immediately. Cards label Curator ranking as `Match Score`, matched keyword chips as `Signals`, and personalized Curator rationale as `Why this matches you`; simple keyword-echo rationales omit extra text when signal pills are present while richer Curator prose remains visible. Parsed user scores accept real values from 1 to 5 and appear as `Your score: N/5` with the same numeric size as the match score. `Scored` means user feedback-backed papers with saved raw or structured feedback, not merely a legacy reviewed status; it includes raw-only feedback after a parse failure and can include records outside the current recommendation queue. The shared Project Paper shell links between Review Queue, `/topics`, and `/health`.

The review queue links to `/health`, and `/health` links back to the review queue and `/topics`. The health page is a compact source-aware operations dashboard for the last 7/21/30/90 days. It computes from raw SQLite facts and shows top operational cards, warning banners, lightweight charts, daily funnel tables, source breakdowns, exclusion reasons, artifact coverage, feedback/profile activity, and source filters without adding aggregate tables. The current graph work is intentionally lightweight; leave heavier charting for later if the need becomes clear.

The same V2 storage path is available from the CLI:

```bash
python3 -m paper_agents.cli feedback add --paper-id 12 --status interested --file /tmp/feedback.txt
```

The Review Queue UI auto-applies the newly submitted structured feedback row to the active profile through Gemini after the feedback blob is safely stored. Existing saved blobs appear in View/edit feedback after `git pull` and a web restart; no migration is required. If Gemini/provider/JSON handling fails, the saved feedback is retained, the structured row remains unapplied, and the UI shows a warning banner. Inspect recent attempts:

```bash
sqlite3 data/paper_agent.db "SELECT id, provider, status, error, profile_version_id, created_at FROM feedback_profile_apply_attempts ORDER BY id DESC LIMIT 10;"
```

Manual profile apply remains available for recovery, testing, and operations. With no explicit model, the Gemini CLI default is tried first and quota/rate-limit failures retry once with `gemini-3.1-flash-lite`:

```bash
python3 -m paper_agents.cli feedback apply --provider gemini --dry-run
python3 -m paper_agents.cli feedback apply --provider gemini
```

Use an explicit model to bypass fallback behavior:

```bash
python3 -m paper_agents.cli feedback apply --provider gemini --model gemini-3.1-flash-lite
```

Gemini profile updates use a 180-second default CLI timeout. If Gemini is slow on the Mac mini, raise it for the retry:

```bash
export PAPER_AGENT_GEMINI_TIMEOUT_SECONDS=240
python3 -m paper_agents.cli feedback apply --provider gemini
```

Failed default-model attempts and fallback successes are both recorded in `feedback_profile_apply_attempts`, so `/health` may show historic failed attempts even after retry success. Profile apply uses the Gemini CLI/API quota path, which is separate from Gemini app usage.

Full rebuild is a manual compression path that reads all structured feedback and creates a fresh compact profile. Do not run it automatically yet:

```bash
python3 -m paper_agents.cli feedback rebuild-profile --provider gemini --dry-run
python3 -m paper_agents.cli feedback rebuild-profile --provider gemini
scripts/compare_feedback_profile_rebuild.sh
scripts/biweekly_profile_rebuild_compare.sh
```

Review profile quality after roughly 10 feedback items or if recommendation quality shows an obvious downward trend. The biweekly wrapper is cron-safe because it only produces a dry-run comparison log and never applies the rebuilt profile. It skips Mondays outside the two-week cadence anchored at `2026-09-07`; set `PAPER_AGENT_PROFILE_REBUILD_ANCHOR=YYYY-MM-DD` to move that cadence.


If older recommended papers have PDFs or usable source abstracts but no triage summaries, backfill those missing summaries without running Scout/Curator again:

```bash
python3 -m paper_agents.cli review-backfill --quick
```

## Nightly Cron

The current intended Mac mini pipeline crontab has three daily source jobs: arXiv at 5:00 AM, Semantic Scholar at 6:00 AM, and OpenAlex at 6:30 AM via the rotating topic script. It also has a biweekly Monday 2:00 AM Gemini profile rebuild comparison that writes a review log but never applies the rebuilt profile. The Semantic Scholar and profile comparison jobs source `$HOME/.bashrc` so API/provider environment is available to cron.

Install or refresh the repo-owned Mac mini crontab after `git pull`:

```bash
scripts/install_project_paper_cron.sh --dry-run
scripts/install_project_paper_cron.sh --apply
```

The installer preserves unrelated cron entries, removes older Project Paper cron lines, and installs the managed block from `deploy/project-paper.crontab`.

```cron
# Daily arXiv Scout/Curator/Reviewer pipeline.
0 5 * * * cd /home/devitolo/workspace/paper-agent && mkdir -p logs && scripts/nightly_pipeline.sh >> logs/pipeline-daily.log 2>&1
# Biweekly Monday Gemini full feedback-profile rebuild comparison; dry-run only, never applies.
0 2 * * 1 . $HOME/.bashrc; cd /home/devitolo/workspace/paper-agent && mkdir -p logs && scripts/biweekly_profile_rebuild_compare.sh >> logs/profile-rebuild-compare.log 2>&1
# Daily OpenAlex Scout/Curator/Reviewer pipeline with rotating configured topics.
30 6 * * * cd /home/devitolo/workspace/paper-agent && mkdir -p logs && scripts/openalex_pipeline.sh >> logs/pipeline-openalex.log 2>&1
# Daily Semantic Scholar Scout/Curator/Reviewer pipeline; sources API key from bashrc.
0 6 * * * cd $HOME/workspace/paper-agent && mkdir -p logs && . $HOME/.bashrc && python3 -m paper_agents.cli pipeline-daily --source semantic_scholar --quick --fetch 3 --keep 1 --max-scout-attempts 1 --request-delay 10 --retries 6 --source-timeout 120 >> logs/pipeline-semantic-scholar.log 2>&1
# Weekly SQLite backup with integrity check.
0 4 * * 0 cd $HOME/workspace/paper-agent && mkdir -p logs && scripts/backup_db.sh >> logs/backup-db.log 2>&1
```

The old one-off Sunday OpenAlex cron that called `pipeline-daily --source openalex` directly has been removed. Do not document or reinstall it; `scripts/openalex_pipeline.sh` is the supported OpenAlex cron entry.

The source jobs rotate across enabled topics in `config/topics.yaml`. Override one OpenAlex run with:

```bash
PAPER_AGENT_OPENALEX_TOPIC="AIOps root cause analysis cloud incidents" scripts/openalex_pipeline.sh
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
