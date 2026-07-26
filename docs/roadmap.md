# Roadmap

This roadmap is phased so the project can remain useful while moving from the current prototype to the target architecture.

## Phase 0: Repository And Design

- Create documentation.
- Confirm current codebase.
- Record architectural decisions.
- Define common data models.

## Phase 1: Manual-Input Proof Of Concept

- Accept paper links or PDFs manually.
- Register papers in SQLite.
- Extract text.
- Run a local-model benchmark.
- Record user feedback.

## Phase 2: Provider-Based Scouting

- Add ChatGPT/Codex adapter.
- Add Gemini adapter.
- Normalize outputs.
- Add run limits and telemetry.
- Compare provider quota per useful paper.

## Phase 3: Automated Acquisition

- Resolve open-access PDF links.
- Download PDFs.
- Deduplicate by identifiers and hash.
- Add retry and failure queues.

## Phase 4: Scheduled Operation

- Add systemd service and timer.
- Add logs and run reports.
- Resume missed jobs.
- Add safe failure recovery.

## Phase 5: Feedback-Driven Ranking

- Build compact preference profiles.
- Retrieve positive and negative examples.
- Improve ranking based on user feedback.

## Phase 6: Optimization

- Tune local/cloud workload split.
- Optimize quotas and costs.
- Evaluate hardware upgrades.
- Add providers only when justified.
