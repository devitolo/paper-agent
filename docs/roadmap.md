# Roadmap

This roadmap is phased so the project can remain useful while moving from the current prototype to the target architecture.

## Phase 0: Repository And Design

- Create documentation.
- Confirm current codebase.
- Record architectural decisions.
- Define common data models.

## Phase 1: Manual-Input Proof Of Concept

Status: Partially complete.

- Accept paper links or PDFs manually through `extract`.
- Register pipeline-discovered papers in SQLite.
- Extract text from PDFs for local-model triage.
- Run a local-model benchmark.
- Record user feedback. V2 blob storage complete; profile learning deferred.

## Phase 2: ArXiv Scout And Curator MVP

Status: Implemented as V2 backend.

- Implement arXiv as the first source adapter.
- Retrieve up to 50 candidates from the last 24 months.
- Store source candidate metadata in `data/scout/YYYY-MM-DD.jsonl` without Scout preference scores.
- Deduplicate candidates and mark previously discovered papers as excluded.
- Move ranking and recommendation decisions into Curator.
- Recommend at most three papers per Curator run.
- Download PDFs and extract triage summaries for recommended papers.
- Record workflow cycles, Scout runs/candidates, Curator evaluations/recommendations, guidance, PDFs, and triage summaries in SQLite.
- Retry arXiv timeouts, 429s, and malformed responses with configurable delay, retries, and timeout.

## Phase 3: Provider-Based Scouting

- Add ChatGPT/Codex paper scout.
- Add Gemini adapter.
- Normalize outputs.
- Add run limits and telemetry.
- Compare provider quota per useful paper.

## Phase 4: Automated Acquisition

- Resolve open-access PDF links beyond arXiv.
- Download PDFs.
- Deduplicate by identifiers and hash.
- Add retry and failure queues for source and PDF failures.

## Phase 5: Scheduled Operation

- Add systemd service and timer.
- Add logs and run reports.
- Resume missed jobs.
- Add safe failure recovery.

## Phase 6: Feedback-Driven Learning

Next major feature area.

- Redesign the Review Queue UI around dense paper triage and blob-first feedback capture. Implemented for first UI pass.
- Store immutable raw ChatGPT discussion summaries. Implemented for V2 blob ingestion.
- Allow multiple parse attempts per raw summary. Implemented for deterministic parser v1.
- Store structured feedback with parser/model provenance. Implemented for deterministic parser v1.
- Track which `structured_feedback` rows have already been applied to profile evolution. Implemented for manual apply.
- Create new profile versions from structured feedback with Gemini. Implemented for manual CLI apply.
- Auto-run Gemini incremental profile apply after Review Queue feedback submit.
- Keep manual `feedback apply --dry-run` and `feedback apply` available for testing and operations.
- Add a manual `feedback rebuild-profile` path that regenerates the compact profile from all structured feedback.
- Review profile quality after 10 feedback items, or earlier if recommendation quality clearly declines.
- Improve Curator ranking based on user feedback.

## Phase 7: Optimization

- Tune local/cloud workload split.
- Optimize quotas and costs.
- Evaluate hardware upgrades.
- Add providers only when justified.
