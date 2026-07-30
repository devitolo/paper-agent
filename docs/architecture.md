# Architecture

This document describes the target Project Paper architecture and the current arXiv-to-SQLite MVP. Some target components, such as provider adapters beyond arXiv and scheduled systemd operation, are still planned.

## Runtime Host

The complete runtime environment is planned to run on:

- Mac mini Late 2012
- Intel Core i7
- 8 GB RAM
- Ubuntu
- Original system had a 1 TB spinning HDD

The canonical local repository checkout for Project Paper is `/Users/vhl/workspace/paper-agent`. Runtime commands, scheduled jobs, and deployment notes should use that path unless the checkout is intentionally moved.

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
systemd timer
      |
      v
Python workflow
      |
      v
Scout source adapters
   |-- arXiv adapter
   |-- future company blog adapters
   `-- future provider adapters
      |
      v
SQLite candidate pool
      |
      v
Curator scoring and recommendations
      |
      v
Recommended PDF download and local extraction
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

The scheduler starts runs and captures basic process status. The first scheduler should be a systemd service and timer on Ubuntu.

The Python workflow owns orchestration between deterministic steps and model-backed judgment steps. It should remain explicit and debuggable before any larger agent framework is considered.

Scout source adapters translate a normalized scouting request into source-specific calls and return normalized paper records. Scout persists candidate pools and diagnostics only; preference scoring and recommendations belong to Curator.

Curator reads the candidate pool, active profile version, history, and guidance. It stores evaluations for every candidate considered and writes at most three ordered recommendations.

The paper registry stores canonical paper identifiers, alternate sources, workflow state, Scout telemetry, Curator evaluations, recommendation records, artifacts, immutable feedback inputs, parse attempts, structured feedback, profile versions, and active scouting guidance. SQLite is the initial store because the system is single-host and benefits from easy inspection and backup.

The downloader resolves and fetches open-access PDFs when available. File transfer should be deterministic code, not an LLM responsibility.

PDF extraction turns downloaded papers into structured text chunks such as title, abstract, introduction, methodology, results, and conclusion when those sections are available.

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
