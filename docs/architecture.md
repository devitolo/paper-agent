# Architecture

This document describes the target Project Paper architecture and the current arXiv-to-SQLite MVP. Some target components, such as provider adapters beyond arXiv and scheduled systemd operation, are still planned.

## Runtime Host

The complete runtime environment is planned to run on:

- Mac mini Late 2012
- Intel Core i7
- 8 GB RAM
- Ubuntu
- Original system had a 1 TB spinning HDD

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
Cloud scout provider adapter
   |-- ChatGPT/Codex client
   |-- Gemini client
   `-- Future provider adapters
      |
      v
Normalized candidate records
      |
      v
Local history and duplicate filtering
      |
      v
Automatic open-access PDF download
      |
      v
PDF text and section extraction
      |
      v
Local LLM scoring and summarization
      |
      v
Optional cloud escalation
      |
      v
User review and feedback
      |
      v
SQLite history and preference updates
```

## Component Responsibilities

The scheduler starts runs and captures basic process status. The first scheduler should be a systemd service and timer on Ubuntu.

The Python workflow owns orchestration between deterministic steps and model-backed judgment steps. It should remain explicit and debuggable before any larger agent framework is considered.

Provider adapters translate a normalized scouting request into provider-specific prompts or API calls and return normalized paper records. Downstream filtering, downloading, scoring, and reporting should not depend on a provider-specific response shape.

The paper registry stores paper identifiers, source metadata, local file paths, artifact records, candidate scores, selected flags, and run telemetry. SQLite is the initial store because the system is single-host and benefits from easy inspection and backup. Feedback rows are reserved for the next feedback loop pass.

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
