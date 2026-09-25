# Architecture

This document describes the target Project Paper architecture and the current
SQLite-backed implementation. The production Mini was migrated to a
containerized app and Ollama runtime on 2026-09-24; host cron remains the
scheduling authority.

## Runtime Host

The complete runtime environment is planned to run on:

- Mac mini Late 2012
- Intel Core i7
- 8 GB RAM
- Ubuntu
- Original system had a 1 TB spinning HDD

The production Mini keeps its operator checkout at
`$HOME/workspace/paper-agent`. The web/Python process and Ollama now run in
separate containers with persistent mounted state. The app is published only on
`127.0.0.1:8000`; the former native web and Ollama services are inactive. This
operator deployment is distinct from the public first-user package.

The Mac mini hosts:

- Scheduler
- Python workflow
- Provider adapters
- Paper registry
- Feedback history
- Downloader
- PDF extraction
- Local model inference
- Logs
- Benchmark records
- Generated reports

## High-Level Architecture

```text
host crontab
      |
      v
container job launcher
      |
      v
app container / Python workflow
      |
      v
Scout source adapters
   |-- arXiv adapter (default/nightly)
   |-- Semantic Scholar adapter (opt-in)
   |-- OpenAlex adapter (opt-in)
   `-- future source adapters
      |
      v
SQLite candidate pool
      |
      v
Curator scoring and recommendations
      |
      v
Reviewer PDF download and local extraction
      |
      v
Manual ChatGPT Paper Discussion
      |
      v
Manual final summary handoff
      |
      v
Feedback Agent and profile versioning
      |
      v
SQLite history and guidance
```

## Component Responsibilities

The production scheduler is the host crontab. Each managed entry calls
`scripts/mini_container_job.sh`, which executes the existing workflow wrapper
inside the app container while participating in the runtime lifecycle lock. The
container runtime owns the long-lived web and Ollama processes; systemd is no
longer the active Project Paper web/model supervisor on the Mini.

The Python workflow owns orchestration between deterministic steps and model-backed judgment steps. It should remain explicit and debuggable before any larger agent framework is considered. A future Go port is plausible, but should wait until workflow and schema boundaries stabilize; preserve SQLite compatibility first, then phase the port through web UI/server, operational CLI/runbooks, scheduled orchestration, and source adapters/agent logic where useful.

Scout source adapters translate a normalized scouting request into source-specific calls and return normalized paper records. arXiv is the default and nightly source; Semantic Scholar and OpenAlex are opt-in adapters for exploratory runs. Scout persists candidate pools and diagnostics only; preference scoring and recommendations belong to Curator.

Curator reads the candidate pool, active profile version, history, guidance, and stored artifact provenance. Normal pipeline scoring first computes deterministic relevance/profile fit, then runs bounded local Qwen evidence assessment per candidate outside SQLite write transactions. It stores evaluations for every candidate considered, persists evidence assessment/provenance/model/timing/score components in run metadata, and writes at most three ordered recommendations.

The paper registry stores canonical paper identifiers, alternate sources, workflow state, Scout telemetry, Curator evaluations, recommendation records, artifacts, immutable feedback inputs, parse attempts, structured feedback, profile apply attempts, profile versions, and active scouting guidance. SQLite is the initial store because the system is single-host and benefits from easy inspection, online backup, and restore drills.

Reviewer resolves and fetches open-access PDFs when available, including current Semantic Reader fallback links, registers PDF artifacts, runs local triage extraction, and stores summary artifacts. If no PDF can be downloaded but source metadata includes an abstract, Reviewer may store an abstract-only triage summary marked as source-metadata fallback rather than full-paper evidence. File transfer should be deterministic code, not an LLM responsibility.

PDF extraction turns downloaded papers into structured triage fields such as paper date, research problem, why it matters, and approach. Abstract-only triage uses the same display shape but should be treated as weaker source metadata, not full PDF reading.

Local model inference handles high-volume scoring and summarization if benchmarks show that a small quantized model performs acceptably on the Mac mini.

Cloud escalation is reserved for discovery and high-value reasoning, such as difficult comparisons, synthesis across the strongest papers, or cases where the local model is unreliable.

## Architectural Principles

- Provider agnostic.
- Local-first for high-volume work.
- Cloud models reserved for discovery and difficult reasoning.
- Deterministic software handles downloading, parsing, filtering, and storage.
- LLMs make judgments rather than perform file transfer.
- Human feedback is persisted and reused.
- Every run is measurable.
- Source adapters should be polite and retry transient failures before giving up.
- Initial implementation should remain simple and debuggable.
- RAFT is not part of this project.
