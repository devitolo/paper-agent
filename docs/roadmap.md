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
- Record user feedback. Not started.

## Phase 2: ArXiv Scout MVP

Status: Implemented.

- Implement arXiv as the first source adapter.
- Retrieve up to 50 candidates from the last 24 months.
- Store all candidate metadata in `data/scout/YYYY-MM-DD.jsonl`.
- Deduplicate candidates.
- Rank candidates with deterministic keywords for AI applied to SRE, operations, observability, incident response, debugging, reliability, and engineering workflows.
- Select the top 5 candidates.
- Download PDFs for the selected candidates only.
- Record Scout runs, candidates, selected flags, PDFs, and triage summaries in SQLite.
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

## Phase 6: Feedback-Driven Ranking

Next major feature area.

- Build compact preference profiles.
- Retrieve positive and negative examples from SQLite.
- Move Scout retrieval topics, positive keywords, domain context terms, and negative keywords into an inspectable preference file.
- Add Feedback Loop v2: convert natural-language paper feedback into proposed preference updates for review before applying them.
- Improve ranking based on user feedback.

## Phase 7: Optimization

- Tune local/cloud workload split.
- Optimize quotas and costs.
- Evaluate hardware upgrades.
- Add providers only when justified.
