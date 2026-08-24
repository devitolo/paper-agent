# Product Changelog

Durable product and process decisions for Project Paper. Keep entries chronological and focused on decisions that should survive across implementation threads.

## 2026-08-22

### Topic Management V1

Status: Implemented and refined.

Decision: Scout topic steering should start as a UI-first, file-backed workflow before automatic Scout learning.

Completed behavior:

- Add `config/topics.yaml` as the human-readable Scout topic config.
- Make `/topics` editable with a one-field fast path for adding topics such as `Datalake operations`.
- Infer default query, sources, cadence, priority, and enabled state for fast-path topics.
- Keep add-topic minimal; detailed query, source, cadence, priority, and enabled controls are available when editing saved topics.
- Keep explicit CLI `--topic` overrides working.
- Let scheduled source jobs select enabled config topics on future runs instead of querying every topic every day.
- Keep `/topics` read/write only for config management; no `Run now` button in V1.
- Refine `/topics` into a collapsed-by-default compact table with one-line rows, source/status/priority chips, quick Enable/Disable actions, and a single focused edit panel.
- Prevent duplicate topic adds/edits by comparing normalized labels and queries.

Backlog:

- Track per-topic funnel performance before adding automatic Scout learning or priority recommendations.
- Future TopicAgent suggestions should use local Ollama/Qwen on the Mini, likely `qwen2.5:1.5b-instruct` unless centralized model config changes. The agent should propose structured topic config only; the user approves before save, and backend validation should reject invalid JSON and fall back to deterministic defaults.

## 2026-08-21

### Review Queue feedback visibility

Status: Implemented.

Decision: Lightweight review status and actual feedback presence are separate concepts and need separate filters.

Completed behavior:

- Keep `Reviewed` as the lightweight status filter.
- Use filter labels in this order: All, Scored, Needs review, Interested, Read later, Reviewed, Not interested.
- Keep the underlying `has_feedback` value for Scored URLs.
- Add `Scored` to show papers with saved structured feedback, or raw feedback if parsing did not produce a structured row.
- Allow `Scored` to include feedback-backed papers outside the current recommendations.
- Show user score, feedback decision, and latest feedback timestamp on feedback-backed cards.

### Decimal feedback scores

Status: Implemented.

Decision: User feedback scores should support real values instead of integers only, so ratings such as `4.5` are preserved.

Completed behavior:

- Change `structured_feedback.score` to `REAL` and migrate existing SQLite databases during init.
- Parse `Score: 4.5` style feedback values when they are within 1 to 5.
- Display decimal user scores as `Your score: N/5` in the Review Queue without confusing them with the Curator system score.

### Non-blocking Review Queue feedback save

Status: Implemented.

Decision: Saving feedback from the browser should not wait on Gemini profile apply, because provider latency or quota problems can otherwise leave the page spinning after feedback has already been persisted.

Completed behavior:

- `/feedback` still stores lightweight feedback, raw feedback, parse attempts, and structured feedback before profile apply.
- Review Queue profile apply now runs in a background worker after non-empty Feedback saves.
- The browser redirects immediately with a saved/queued banner; profile apply successes or failures remain visible through existing profile apply attempt records and health checks.

### Read-only Scout topic inventory

Status: Implemented.

Decision: Before automatic Scout learning or topic editing exists, the operator needs visibility into the configured source topic lists.

Completed behavior:

- Add `/topics` to the existing Review Queue web server.
- Link Review Queue, Health, and Topics pages together.
- Show arXiv daily default topics, OpenAlex weekly rotating script topics, and documented Semantic Scholar cron topic visibility.
- Keep the page read-only and label cron/operator overrides as external when they are not stored in the repo.

Backlog:

- Make Scout topics user-editable later, either from the UI or a simple config file.
- Include per-source topic selection, priority/cadence controls, and ad hoc one-off topics such as `datalake`.
- Track topic performance over time before enabling automatic Scout learning.

## 2026-08-19

### Gemini profile apply timeout and fallback recovery

Status: Implemented in commits `6745d8d` and `ad85bfc`.

Decision: Gemini profile updates on the Mac mini need longer timeouts and a quota/rate-limit fallback path, while preserving operator control over explicit model choices.

Completed behavior:

- `feedback apply --provider gemini` without `--model` uses the Gemini CLI default model first.
- Quota/rate-limit failures retry once with `gemini-3.1-flash-lite`.
- Explicit `--model` choices are honored without automatic fallback.
- Gemini CLI timeout defaults to 180 seconds and can be changed with `PAPER_AGENT_GEMINI_TIMEOUT_SECONDS`.
- Failed default-model attempts and fallback success attempts are both recorded in `feedback_profile_apply_attempts`, so `/health` may show historic failed attempts even after retry success.
- Profile apply uses the Gemini CLI/API quota path; Gemini app usage and Gemini API/AI Studio quota screens are different operational views.

Backlog:

- Dry-run currently calls Gemini, and real apply calls Gemini again. Consider caching a reviewed dry-run proposal to reduce quota use.

## 2026-08-18

### System health and pipeline metrics dashboard

Status: Implemented.

Decision: Project Paper needs a compact health view that shows whether the Mac mini and recommendation pipeline are working at a glance.

Completed behavior:

- Add `python3 -m paper_agents.cli db health` with `--days`, `--source`, and `--json`.
- Add `/health` to the existing Review Queue web server, with links between the review queue and health dashboard.
- Compute DB integrity, latest workflow cycle age/state, days since last recommendation, source-aware Scout/Curator/Reviewer funnel counts, artifact gaps, feedback/profile status, and operator warnings directly from raw SQLite facts instead of materialized aggregate tables.
- Include lightweight charts and tables for daily candidates, eligible papers, recommendations, source breakdowns, feedback/profile activity, and exclusion reasons.
- Surface stale workflow cycles, zero-candidate/zero-eligible Scout runs, missing triage summaries, unapplied structured feedback, recent Gemini/profile apply failures, and DB integrity failures.
- Add compact navigation between the Review Queue and `/health`.
- Add inline SVG charts for daily funnel trends, source breakdown, recommendation gaps, and feedback/profile activity.

Future consideration:

- Add Mac mini CPU, temperature, disk, backup freshness, and tunnel metrics if they become easy to collect without making the dashboard heavy.

### Scout sources: Semantic Scholar, OpenAlex, and source-aware review

Status: Implemented in commits `be36bbe`, `865e1ab`, `c14e9d9`, and `b66a7a6`.

Decision: Project Paper can support multiple Scout sources as opt-in adapters while keeping arXiv as the default and nightly cron source.

Completed behavior:

- Semantic Scholar can be selected with `--source semantic_scholar`.
- Semantic Scholar reads `SEMANTIC_SCHOLAR_API_KEY` and sends it as the `x-api-key` header; direct CLI testing with the approved key succeeded. Use `--request-delay 2` or higher because approved key guidance is 1 request per second cumulatively across endpoints.
- OpenAlex can be selected with `--source openalex` and does not require an API key.
- Non-arXiv sources are opt-in and are not part of nightly cron yet.
- Review Queue cards show source badges, and the source filter composes with status, sort, and view controls.

Follow-up implementation:

- OpenAlex now narrows searches toward software/cloud/operations context, requests article-like work types, and filters obvious book/index/reference and biomedical noise before storage.
- Review Queue cards show a clearly labeled source abstract from stored source metadata when local triage extraction is missing.
- Same-cycle Scout rediscoveries remain eligible during bounded rescouts; older-cycle discoveries are still excluded as `previously_discovered`.
- OpenAlex has a separate Monday 6:30 AM rotating cron script, `scripts/openalex_pipeline.sh`, with fetch 30, keep 2, and `--max-scout-attempts 1` so stable OpenAlex queries do not exhaust the same tiny pool every day.
- The old direct one-off Sunday `pipeline-daily --source openalex` cron has been removed and is superseded by the script.
- Semantic Scholar has a separate Tuesday 6:00 AM cron using `--source semantic_scholar`, `--request-delay 2`, fetch 10, keep 2, and `--max-scout-attempts 1`; the job sources `$HOME/.bashrc` for the API key.


### Review Queue quick status and score clarity

Status: Implemented in commits `4698a43` and `25ee9b2`.

Completed behavior:

- Quick status buttons (`Interested`, `Read later`, `Not interested`, `Reviewed`) save status only and avoid feedback ingestion/profile apply.
- Saving a non-empty Feedback blob is the only Review Queue path that ingests feedback and triggers Gemini profile apply.
- Quick status submit shows `Saving...` and a visible timeout hint if completion hangs.
- Cards label Curator ranking as `System` score.
- Parsed user feedback scores render as `Your score: N/5` and visually demote the system score.
- Source badges have distinct compact styling for arXiv, OpenAlex, Semantic Scholar, and unknown sources.

## 2026-07-31

### Operability: backups, apply attempts, and logs

Status: Implemented in commit `0436c19`.

Decision: The Mac mini runtime needs recoverable SQLite backups, visible Gemini profile-apply failures, and bounded cron logs before the feedback loop becomes routine.

Completed behavior:

- `scripts/backup_db.sh` creates timestamped online SQLite backups under `backups/` by default.
- Backups use `sqlite3.Connection.backup()` and verify with `PRAGMA integrity_check`; failed backups are removed and exit nonzero.
- `backups/` is ignored by git.
- `feedback_profile_apply_attempts` records manual and automatic Gemini profile-apply successes and failures.
- If Review Queue profile auto-apply fails, raw and structured feedback remain saved and unapplied for retry.
- The Review Queue redirects with a warning banner telling the operator to run `feedback apply` manually when ready.
- `deploy/project-paper.logrotate` provides weekly compressed rotation for `logs/*.log`.
- `scripts/tail_logs.sh` tails local Project Paper logs.

Operational guidance:

- Run database backups weekly before the daily recommendation pipeline.
- Use the apply-attempt table to inspect recent Gemini failures and recover with manual dry-run/apply.
- Treat restore testing as an operator responsibility before relying on backups for disaster recovery.

## 2026-07-30

### Review Queue UI: denser review inbox

Decision: The Review Queue UI should become denser and more review-inbox-like so paper triage is faster and less visually noisy.

Product direction:

- Reduce overall font sizes and spacing.
- Make header controls more compact.
- Replace the large status filter button group with a dropdown/select.
- Replace latest vs score sorting with a compact control, icon button pair, or small select.
- Replace full vs condensed view with a dropdown/select.
- Prioritize quick review actions on paper cards and reduce visual clutter.
- Add an easy copy icon/button for the original article link on each paper card.
- Remove the visible "No ChatGPT review yet" placeholder.
- Remove the "Copy prompt/link" toggle because it is not needed in the current workflow.
- Show the score as just the number, without the word "score."
- Move Interested, Read later, Not interested, and Reviewed into a compact vertical action rail on the right side under the score.
- Rename the Notes textarea to Feedback.

### Feedback workflow: blob-first

Decision: Feedback should be blob-first rather than a highly structured form as the main user path.

Normal workflow:

1. Open the paper queue.
2. Pick a paper.
3. Discuss and summarize it in ChatGPT.
4. Paste the resulting feedback blob back into Project Paper.

Product direction:

- The Feedback box is where the user pastes the feedback blob from a ChatGPT paper discussion.
- The first UI version may keep using the existing lightweight feedback storage/table for status plus notes, but visually and conceptually the field should be Feedback.
- CLI and UI should eventually share one backend feedback ingestion function.
- Future V2 feedback ingestion should store the exact blob in `raw_feedback`, create a `feedback_parse_attempt`, and write normalized output into `structured_feedback`.
- Do not require the V2 parser/profile update in the first UI redesign; capturing the blob cleanly comes first.

### Feedback ingestion: deterministic V2 storage first

Status: Completed in commit `47c030b`.

Decision: The first V2 feedback ingestion step captures and deterministically parses blobs without changing future recommendations yet.

Completed behavior:

- The review queue Feedback box stores non-empty pasted blobs through the shared V2 ingestion path.
- Existing lightweight review status storage remains in place.
- Exact pasted blobs are stored in `raw_feedback` and deduped by content hash.
- Deterministic parser v1 records `feedback_parse_attempts` and writes normalized rows to `structured_feedback`.
- Parser v1 recognizes simple lines such as `Decision: keep|maybe|reject|interested|read later|not interested|reviewed` and `Score: 1-5`.
- `profile_versions`, ScoutAgent, and CuratorAgent do not consume structured feedback yet.

### Feedback profile evolution: Gemini apply step

Decision: Profile evolution from structured feedback should be explicit, reviewable, and tracked.

Completed behavior:

- Add `feedback_profile_applications` to link consumed `structured_feedback` rows to generated `profile_versions` rows.
- Add `feedback apply --provider gemini --dry-run` so the operator can preview Gemini's proposed profile update.
- Add `feedback apply --provider gemini` to create a new active profile version and mark the consumed structured feedback rows as applied.
- Keep profile evolution out of the review queue POST path.
- Keep ScoutAgent and CuratorAgent reading active `profile_versions` rather than consuming `structured_feedback` directly.

Operational guidance:

- Run dry-run first, inspect the proposed profile and change summary, then apply.
- Gemini is the first low-volume synthesis provider; other providers can be added behind the same provider boundary later.

### Feedback fast loop: auto incremental profile apply

Status: Implemented in commit `894593b`.

Decision: Feedback submission should become the fast learning loop. After the user pastes a feedback blob in the Review Queue UI, Project Paper should store raw and structured feedback, then automatically run a Gemini incremental profile update after submit.

Completed behavior:

- Review Queue feedback submit stores raw and structured feedback, then runs Gemini incremental profile apply.
- Existing `profile_versions` rows remain; each successful apply creates a new profile version with a `change_summary`.
- Manual CLI dry-run/apply remains available for testing and operations.
- The profile stays compact and human-editable; raw feedback may grow, but the active profile remains compressed into durable preferences.
- Gemini is used for low-volume profile synthesis to split provider/token usage.
- Local Qwen remains focused on high-volume extraction and triage.
- ChatGPT remains focused on human paper discussion.

Full rebuild concept:

- Incremental apply is like an incremental build: current profile plus new feedback produces the next profile.
- Full rebuild is like a full build: all structured feedback regenerates a compact profile.
- Full rebuild is available as a manual path at first, roughly monthly, after about 10 new feedback items, or when recommendation quality obviously drifts.

Action item:

- Review profile quality after 10 feedback items, or earlier if recommendation quality shows a clear downward trend.

### Operating model: split roles

Decision: Project Paper work should be split across durable roles so product direction, implementation, operations, and planning do not blur together.

Role ownership:

- Architect thread owns product direction, workflow decisions, schema/agent boundaries, and deciding what should exist and why.
- Developer thread owns implementation, tests, commits/pushes, and small docs tied directly to code changes.
- DevOps/SRE thread owns Mac mini operations, cron/systemd, logs, health checks, backups, tunnels, and networking.
- Tech Doc Editor thread owns durable project memory: product changelog, decision log, roadmap cleanup, README/workflow/architecture consistency.
- SDLC thread owns milestones, release planning, issue breakdown, and definition of done.

### Architecture state: current MVP pipeline

Current state: The MVP pipeline is `ScoutAgent -> CuratorAgent -> ReviewerAgent`, orchestrated by `pipeline-daily`.

Responsibilities:

- Scout retrieves candidates and stores metadata/history.
- Curator scores candidates and creates recommendations.
- Reviewer downloads PDFs, runs local triage extraction, and stores artifacts.
- Feedback Agent and V2 feedback ingestion are the next workflow area, but the immediate product focus is the Review Queue UI redesign for the feedback blob path.
