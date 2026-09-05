# Product Changelog

Durable product and process decisions for Project Paper. Keep entries chronological and focused on decisions that should survive across implementation threads.

## 2026-09-04

### Abstract-only triage fallback

Status: Implemented in commits `83fe25a`, `1b7396e`, `6209168`, and `6a7d4ac`.

Decision: Recommended papers should stay reviewable when no full PDF can be downloaded, as long as source metadata includes an abstract, while clearly labeling that this is not full-paper evidence.

Completed behavior:

- Try Semantic Reader fallback links before giving up on Semantic Scholar PDF download.
- Create structured abstract-only `triage_summary` artifacts from source abstracts when no PDF can be downloaded.
- Mark abstract-only artifacts in JSON and artifact metadata.
- Show `Abstract-only triage. Full PDF was not downloaded.` inside the `Why this matches you` panel.
- Let successful abstract-only backfill clear Health's missing-triage warning while preserving artifact gap visibility when no PDF exists.
- Keep recommended non-downloadable papers visible in the Review Queue instead of silently hiding them.

## 2026-09-03

### Semantic Scholar PDF-link handling

Status: Implemented.

Decision: Semantic Scholar/OpenAlex records should remain visible when the source provides a DOI or landing-page URL, but those URLs should not be presented as directly openable PDFs.

Completed behavior:

- Render `Open PDF` only for real PDF artifacts or plausibly direct PDF links.
- Keep source abstracts as a fallback only for active-reviewable cards whose local triage extraction is missing.
- Preserve Semantic Scholar/OpenAlex candidate and queue visibility when the source-provided PDF field is DOI-like, while avoiding the misleading `Open PDF` control for DOI-only URLs.
- Roll back the earlier Sep 1 rule that hid or excluded non-arXiv papers solely because they lacked a PDF URL.
- Use Semantic Reader as a reviewer/backfill fallback: derive `/reader/{paperId}`, parse its download link, and save the linked PDF only when the response is real PDF content.
- Add a biweekly Monday 2:00 AM cron wrapper for Gemini full feedback-profile rebuild comparisons. The job is review-only: it runs the rebuild in dry-run comparison mode and does not apply the proposed profile.
- Add a repo-owned `deploy/project-paper.crontab` and `scripts/install_project_paper_cron.sh` so `git pull` carries the canonical Mini schedule and the operator can refresh cron without hand-copying individual lines.
- Change the managed Mini source schedule so OpenAlex and Semantic Scholar run daily again, matching the user's desired source coverage.

## 2026-09-01

### Mini health warning runbooks

Status: Implemented.

Decision: Common `/health` warnings should have focused Mini scripts so routine recovery does not require ad hoc SQLite spelunking.

Completed behavior:

- Treat Health warnings as actionable work, not historical noise, and hide the warning section when there are no warnings.
- Add `scripts/health_warnings.sh` as the first command to run when `/health` shows warnings; it prints the normal health summary plus detailed rows behind missing triage summaries, recent profile apply failures, and unapplied structured feedback.
- Add `scripts/fix_missing_triage_summaries.sh --quick --limit 5` as a safe wrapper around `review-backfill` for recommendations with PDFs, without rerunning Scout/Curator.
- Add `scripts/apply_pending_feedback_profile.sh`, where `--dry-run` previews pending profile apply and `--apply` writes the profile update.
- Clear the Gemini/profile failure warning once a later non-dry-run profile apply succeeds.
- Warn about missing triage summaries only when a latest-cycle recommendation has an available PDF or direct PDF URL; DOI-only recommendations do not create an unrecoverable warning.
- Keep scripts non-destructive; they do not delete rows, reset the DB, or rerun Scout.
- Document the operator mental model: DB changes appear on browser refresh, while pulled code/template/static changes require restarting the long-running Python web server; cached static assets may need a hard refresh.

### Review Queue saved feedback editing

Status: Implemented in commit `9d931f3`.

Decision: Scored papers should reopen with the original saved feedback blob ready to revise, so the Feedback box remains the durable Paper Discussion handoff surface.

Completed behavior:

- Prefill View/edit feedback from the latest `raw_feedback.content` blob, with legacy `feedback.notes` as fallback for older records.
- Keep `structured_feedback` as the source for score, decision, timestamp display, and the Scored filter.
- Keep `Scored` tied to saved raw or structured feedback, not merely a legacy reviewed status.
- Require no data migration; after `git pull` and web restart, existing saved blobs appear in the editor.

## 2026-08-31

### Feedback-guided Scout retrieval

Status: Implemented in commit `f128fcd`.

Decision: Scout should begin using the active profile and recent structured feedback deterministically, without adding an LLM ScoutAgent or deep paper reading yet.

Completed behavior:

- Build per-run Scout guidance from active `profile_versions` plus recent `structured_feedback`.
- Add boost/include terms from high-scored `keep` feedback and avoid terms from low-scored `reject` feedback.
- Expand source query topics with a small bounded set of feedback-derived boost terms.
- Record the exact guidance used in `scouting_guidance` and `scout_runs.diagnostics_json`.
- Exclude only clear avoid-heavy candidate matches with no positive guidance hits; otherwise keep penalties visible in source diagnostics.
- Print guidance summaries from `scout-daily` and `pipeline-daily` logs.
- Keep Curator responsible for recommendation scoring; Scout guidance only affects retrieval expansion and conservative prefiltering.
- Keep penalties conservative while the feedback sample is small, around 10 scored papers.

Out of scope for this iteration:

- No new sources, no LLM Scout agent, no UI changes, and no deep paper/PDF reading.
- Deep evidence-aware paper/PDF reranking remains planned for Curator V2.

## 2026-08-29

### Review Queue card layout and logo polish

Status: Implemented in commits `cfdfa1c` and `ab237ee`.

Decision: The Review Queue should keep paper selection, source context, and feedback as the primary workflow while moving secondary affordances out of the main action path.

Completed behavior:

- Separate header navigation from queue controls so the top band stays compact.
- Remove the Apply button, visually hide filter labels, and replace the View dropdown with a Full/Condensed two-button toggle.
- Make the paper title the primary open affordance and keep Open PDF and Copy next to source/date/source-id metadata.
- Add `Copy discussion prompt` as a lightweight handoff to the external Paper Discussion ChatGPT conversation.
- Remove the visible Open paper / Not interested action cluster from paper cards.
- Remove Open summary/raw JSON from regular cards.
- Label matched keyword pills as `Signals`.
- Keep `Why this matches you` for personalized Curator reasoning, while omitting extra keyword-echo rationale text when signal pills are present.
- Preserve All papers, Scored, and Needs review filters, source/sort/view controls, Feedback add/view/save behavior, and backend-only lightweight status compatibility.
- Replace `logo_light.png` and `logo_dark.png` assets while preserving their filenames.
- Add cache-busted logo URLs so browser refreshes pick up replaced assets after deploy.
- Crop/zoom the header logo in CSS so the center mark reads larger.

## 2026-08-28

### Review Queue workflow simplification

Status: Implemented.

Decision: The Review Queue should optimize for choosing a paper, opening/copying it, discussing it externally, and returning to save feedback.

Completed behavior:

- Use a shared Project Paper shell/header across Review Queue, Topics, and Health.
- Default Review Queue sorting to Highest score, with Newest still available.
- Keep source filtering and source badges.
- Remove Read later, Interested, explicit Reviewed, and Not interested from primary paper-card actions.
- Limit the Review Queue filter dropdown to All, Scored, and Needs review.
- Add a visual identity pass that keeps the dense dark UI while treating Curator ranking as `Match Score` and rationale as `Why this matches you`.
- Move paper opening to the title, keep Copy with the source/date/source-id metadata, and collapse artifact links out of the regular action path.
- Hide empty feedback inputs behind Add feedback; show View/edit feedback when feedback exists.
- Keep `Scored` as the main way to find reviewed/feedback-backed papers.
- Preserve historical lightweight status values in compatibility logic without showing them as primary controls.
- Add `db cleanup-legacy-feedback` so old `interested`, `read_later`, and `reviewed` rows can be removed from the legacy `feedback` table while preserving `not_interested`, `raw_feedback`, and `structured_feedback`.
- Saving a non-empty feedback blob now avoids writing a duplicate legacy lightweight status row.

## 2026-08-22

### Topic Management V1

Status: Implemented and refined.

Decision: Scout topic steering should start as a UI-first, file-backed workflow before automatic Scout learning.

Completed behavior:

- Add `config/topics.yaml` as the human-readable Scout topic config.
- Make `/topics` the Topic Management page, with source schedule inventory above All Topics.
- Make the default flow a Qwen-powered TopicAgent prompt for adding, updating, disabling, or removing topics from natural language.
- Keep TopicAgent prompt, transcript, clarifying questions, and proposal inside one conversational panel.
- Use local Ollama/Qwen on the Mini, defaulting to `qwen2.5:1.5b-instruct`, to propose structured topic config.
- Support TopicAgent actions `create_new`, `update_existing`, `remove_existing`, and `ask_clarifying_question`; disabling is an update with `enabled: false`.
- Require user approval before saving any TopicAgent proposal.
- Validate model JSON and fall back to deterministic proposal logic when the model response is invalid or unavailable.
- Keep manual detailed query, source, cadence, priority, and enabled controls available as an edit escape hatch.
- Keep explicit CLI `--topic` overrides working.
- Let scheduled source jobs select enabled config topics on future runs instead of querying every topic every day.
- Keep `/topics` read/write only for config management; no `Run now` button in V1.
- Refine `/topics` into a collapsed-by-default compact All Topics table with one-line rows, source/status/priority chips, quick Enable/Disable actions, and a single focused edit panel.
- Prevent duplicate topic adds/edits by comparing normalized labels and queries.
- Treat natural-language `remove`/`delete`/`drop` as physical removal from `config/topics.yaml`, while `disable`/`turn off`/`pause` preserves the topic with `enabled: false`.
- Make TopicAgent's Qwen prompt semantic rather than keyword-driven; deterministic fallback is a safety path for safe creates/duplicates/explicit actions and asks a clarifying question for ambiguous change/remove intent instead of guessing.
- Preview pending TopicAgent changes in the topic list before Apply.
- Keep `/topics` scoped to future scheduled runs; no Scout run starts from the UI.

Backlog:

- Track per-topic funnel performance before automatic priority recommendations.

## 2026-08-21

### Review Queue feedback visibility

Status: Implemented.

Decision: Lightweight review status and actual feedback presence are separate concepts and need separate filters.

Completed behavior, superseded by the 2026-08-28 simplification:

- Earlier UI exposed `Reviewed` as a lightweight status filter.
- Earlier filter labels included All, Scored, Needs review, Interested, Read later, Reviewed, and Not interested.
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
- Display decimal user scores as `Your score: N/5` in the Review Queue without confusing them with the Curator match score.

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

- Earlier quick status buttons saved status only and avoided feedback ingestion/profile apply; this was later narrowed to `Not interested` only.
- Saving a non-empty Feedback blob is the only Review Queue path that ingests feedback and triggers Gemini profile apply.
- Quick status submit shows `Saving...` and a visible timeout hint if completion hangs.
- Cards now label Curator ranking as `Match Score`.
- Parsed user feedback scores render as `Your score: N/5` and visually demote the match score.
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
- Earlier design explored moving all quick statuses into a compact vertical action rail; this was later narrowed to `Not interested` only.
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
