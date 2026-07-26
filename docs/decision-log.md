# Decision Log

This log uses an Architecture Decision Record style. Status values describe the design decision, not whether the feature is implemented in the current prototype.

## ADR-001: Mac mini is the runtime host

Status: Accepted

Decision:

The full Project Paper runtime environment will run on the Ubuntu-based Mac mini.

Rationale:

- It is available as an always-on machine.
- It can host scheduling, storage, downloads, SQLite, extraction, logs, and small local models.
- Centralizing runtime simplifies file handling and automation.

Tradeoffs:

- The 2012 Intel hardware and 8 GB RAM limit local model size and inference speed.
- Cloud escalation remains necessary for difficult tasks.

## ADR-002: Provider-agnostic scouting

Status: Accepted

Decision:

The scouting workflow will use a common provider interface and support ChatGPT and Gemini as interchangeable providers.

Rationale:

- The user has access to both.
- Quotas and performance differ.
- The project should not be tied to one vendor.

## ADR-003: SQLite for initial persistence

Status: Proposed

Decision:

Use SQLite for the first implementation.

Rationale:

- Single-host runtime.
- Simple deployment.
- Adequate for paper metadata, history, feedback, and telemetry.
- Easy backups and inspection.

Revisit when:

- Concurrent writers become a problem.
- Remote access is required.
- Dataset size or query patterns exceed SQLite's practical limits.

## ADR-004: systemd timer for scheduling

Status: Proposed

Decision:

Use a systemd service and timer instead of cron for scheduled runs.

Rationale:

- Better logging.
- Failure visibility.
- Environment management.
- Restart behavior.
- Ability to run missed jobs after downtime.

## ADR-005: Local model selected by benchmark

Status: Accepted

Decision:

Do not commit to Qwen or any other local model until models are tested on actual Project Paper tasks.

Rationale:

- Public benchmarks do not measure this workload directly.
- The Mac mini has strict RAM and CPU constraints.
- The right model depends on JSON reliability, ranking agreement, summary quality, and runtime on the target host.

## ADR-006: RAFT excluded

Status: Accepted

Decision:

RAFT is not part of Project Paper.

Rationale:

The initial workflow does not require a multi-agent framework. Plain Python and explicit workflow stages will be simpler to build and debug.
