# AI Stack

This document describes the target AI stack strategy. The current prototype uses OpenAI only; Gemini, local models, and provider-agnostic interfaces are planned work.

## Cloud Providers

The scouting layer must support interchangeable providers.

Initial target providers:

- ChatGPT/Codex client
- Gemini client

Possible future providers:

- OpenAI API
- Gemini API
- Anthropic API
- Other local or hosted models

Provider selection should be configuration-driven:

```yaml
scout:
  provider: chatgpt
  max_queries: 3
  max_candidates: 40
```

Switching providers should not require changes to downstream processing.

## Common Provider Interface

Each provider should accept a normalized request and return normalized paper records.

Example request:

```json
{
  "topic": "multi-agent systems for scientific literature review",
  "max_queries": 3,
  "max_candidates": 40,
  "date_range": "last 12 months",
  "exclude_seen": true
}
```

Example normalized result:

```json
{
  "title": "Example paper",
  "authors": ["A. Researcher"],
  "year": 2026,
  "doi": "10.xxxx/example",
  "arxiv_id": null,
  "source_url": "https://example.org/paper",
  "pdf_url": "https://example.org/paper.pdf",
  "abstract": "Paper abstract",
  "provider": "chatgpt",
  "search_run_id": "run-id"
}
```

The current `ResearchScout` can inform this interface, but it should be refactored away from direct OpenAI coupling before additional providers are added.

## Local Model Strategy

Do not hard-code the local LLM yet.

Model selection is a benchmark task. Candidate model families may include:

- Qwen
- Gemma
- Phi
- Small Llama-family models
- Other compact quantized models

The model must be tested on the actual Project Paper workload.

Target tasks:

- Title and abstract relevance scoring
- Structured metadata extraction
- Short summaries
- Preference-based ranking
- Duplicate or near-duplicate judgment
- Identification of methodology and claims

The Mac mini is expected to support only small quantized models comfortably. Deep synthesis should remain a cloud task unless benchmarks prove otherwise.

## Cost And Quota Strategy

Treat ChatGPT and Gemini subscription quotas as limited premium resources.

Use a funnel:

```text
Broad deterministic search
      |
      v
Local duplicate and history filters
      |
      v
Cloud review of titles and abstracts
      |
      v
Download selected papers
      |
      v
Local model processing
      |
      v
Cloud escalation for only the best or hardest papers
```

Initial trial budget:

- Maximum 3 research queries
- Maximum 40 candidate abstracts
- Maximum 10 selected downloads
- Maximum 5 cloud escalations
- One final result summary

These are starting limits, not permanent settings.

Primary metric:

> Provider quota or cost per new, useful paper.
