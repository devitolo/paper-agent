# Project Paper

Project Paper is a system for discovering, downloading, filtering, scoring, and recommending research papers based on a user's evolving interests and feedback.

The repository currently contains a small Python prototype called `paper_agents`. The prototype searches arXiv, uses OpenAI to score and curate candidates, and stores a human-editable preference profile in JSON. The broader Project Paper architecture described here is the target direction, not the current implementation.

## Overview

Project Paper is intended to become a provider-agnostic, hybrid local/cloud paper discovery and curation platform. The full runtime environment is planned to live on a late-2012 Mac mini running Ubuntu.

The first production-oriented implementation should favor simple Python, SQLite, explicit workflow stages, and systemd timers over a heavy agent framework.

## Architecture Summary

- The Mac mini is the runtime host.
- Scheduled workflows run locally on Ubuntu.
- Cloud AI providers are used selectively for paper scouting and higher-value reasoning.
- Local models handle high-volume filtering, scoring, summarization, and extraction where benchmarks show they are effective.
- The workflow should support switching between ChatGPT and Gemini without rewriting downstream processing.
- Previously seen, accepted, declined, and highly rated papers are stored and reused in future filtering.
- Open-access PDFs should be downloaded automatically when available.
- Deterministic code should handle downloading, parsing, filtering, and storage; LLMs should make judgments rather than perform file transfer.

See [docs/architecture.md](docs/architecture.md) and [docs/ai-stack.md](docs/ai-stack.md) for the target design.

## Current Project Status

Implemented today:

- `ResearchScout` queries arXiv for recent papers based on `data/profile.json`.
- `scout-daily` runs a deterministic arXiv Scout MVP with keyword ranking, JSONL metadata storage, top-five selection, PDF downloads for selected papers, and retry/backoff controls for arXiv requests.
- `pipeline-daily` runs Scout, downloads selected PDFs, extracts local triage cards with Ollama, and records runs, papers, candidates, PDFs, and summary artifacts in SQLite.
- `review-summary` creates a ChatGPT section-by-section Markdown review from a triage summary.
- OpenAI scores candidate titles and abstracts in the older `run` prototype.
- `ResearchCurator` selects a short reading list from the scout output.
- `FeedbackAgent` updates `data/profile.json` from natural-language feedback.
- Docker can run the CLI with a mounted `.env` and `data/` directory.

Not implemented yet:

- Feedback Loop v2 backed by SQLite feedback rows.
- Gemini or other provider adapters.
- General open-access PDF resolution beyond arXiv.
- Duplicate/history filtering backed by the SQLite registry.
- systemd service and timer.
- Benchmark recording and generated run reports.

## Planned Workflow

1. A systemd timer starts the workflow on the Mac mini.
2. The workflow loads configuration, paper history, and preference data.
3. A configured cloud scout provider finds candidate papers.
4. Candidate records are normalized into a provider-independent schema.
5. Local filters remove duplicates and papers already seen, accepted, declined, or recently reviewed.
6. Open-access PDFs are downloaded when available.
7. Downloaded files are hashed and registered.
8. PDF text and useful sections are extracted.
9. A benchmark-selected local model scores and summarizes selected content.
10. Strong or difficult candidates may be escalated to a cloud model.
11. A report is generated for user review.
12. User feedback is written locally and reused in future runs.

The current implementation covers the arXiv Scout MVP, local Ollama triage extraction, SQLite registry writes and inspection commands, ChatGPT review generation, and the older OpenAI scout/curator prototype.

## Requirements

Current prototype:

- Python 3.12 or Docker
- OpenAI API key
- Network access to arXiv and the OpenAI API

Target runtime:

- Mac mini Late 2012, Intel Core i7, 8 GB RAM, running Ubuntu
- Python
- SQLite
- systemd
- PDF download and extraction tooling
- A small local model runtime selected through benchmarking
- Cloud provider access for ChatGPT/Codex, Gemini, or future providers

## Run The Current Prototype

Add your API key to `.env`:

```bash
OPENAI_API_KEY=your_api_key
OPENAI_MODEL=gpt-5.1
```

Build the container:

```bash
docker compose build
```

Discover and curate papers:

```bash
docker compose run --rm paper-agents run
```

Give feedback after reviewing the recommendations:

```bash
docker compose run --rm paper-agents feedback "This was too theoretical. I want more practical real-world incident management papers."
```

Inspect the stored preference profile:

```bash
docker compose run --rm paper-agents profile
```

You can still run locally with Python:

```bash
python3 -m paper_agents.cli run
```

Run the deterministic arXiv Scout MVP:

```bash
python3 -m paper_agents.cli scout-daily
```

The first Scout implementation uses arXiv only, stores all candidate metadata in `data/scout/YYYY-MM-DD.jsonl`, ranks candidates with deterministic keywords, keeps the top 5, and downloads PDFs for the selected papers into `data/papers/arxiv/`. The default Scout run fetches up to 50 candidates across the default topic set.

For a gentle arXiv test, use a single topic and the network hardening flags:

```bash
python3 -m paper_agents.cli scout-daily \
  --topic "incident management" \
  --fetch 3 \
  --keep 1 \
  --no-download \
  --request-delay 5 \
  --retries 4 \
  --source-timeout 90
```

Initialize the SQLite registry:

```bash
python3 -m paper_agents.cli db init
```

Inspect the registry:

```bash
python3 -m paper_agents.cli db stats
python3 -m paper_agents.cli db recent-runs --limit 5
python3 -m paper_agents.cli db papers --selected --limit 10
```

Run the daily Scout-to-triage pipeline:

```bash
python3 -m paper_agents.cli pipeline-daily --fetch 20 --keep 3
```

This runs Scout, downloads the selected PDFs, extracts local triage cards with Ollama, saves summaries under `data/extractions/`, records runs and artifacts in `data/paper_agent.db`, and prints a compact review list. The default is full mode for scheduled runs. Use `--no-db` for throwaway runs that should not touch the SQLite registry. For an interactive preview, use quick mode:

```bash
python3 -m paper_agents.cli pipeline-daily \
  --quick \
  --fetch 20 \
  --keep 3 \
  --request-delay 5 \
  --retries 4 \
  --source-timeout 90
```

Quick mode extracts only the first 2 chunks per selected paper unless `--limit-chunks` is set explicitly.

Create a ChatGPT section-by-section review from one triage summary:

```bash
python3 -m paper_agents.cli review-summary data/extractions/arxiv/2607.07052v1.qwen2.5-1.5b-instruct.summary.json
```

This writes a listening-friendly Markdown review under `data/reviews/`. The review includes an `Audio Notes` section that can later feed a text-to-speech step.

Stable local output folders:

- Scout metadata: `data/scout/YYYY-MM-DD.jsonl`
- Downloaded PDFs: `data/papers/SOURCE/`
- Local extraction summaries: `data/extractions/SOURCE/`
- ChatGPT paper reviews: `data/reviews/SOURCE/`
- SQLite registry: `data/paper_agent.db`

Extract structured paper metadata locally with Ollama:

```bash
python3 -m paper_agents.cli extract paper.pdf --model qwen2.5:1.5b-instruct
```

## Repository Layout

```text
.
|-- data/
|   `-- profile.json
|-- deploy/
|   `-- mini.sh
|-- docs/
|   |-- ai-stack.md
|   |-- architecture.md
|   |-- benchmarking.md
|   |-- decision-log.md
|   |-- roadmap.md
|   `-- workflow.md
|-- paper_agents/
|   |-- cli.py
|   |-- curator.py
|   |-- db.py
|   |-- feedback.py
|   |-- local_extract.py
|   |-- openai_helpers.py
|   |-- pipeline.py
|   |-- review.py
|   |-- scout.py
|   `-- store.py
|-- scripts/
|   |-- paper_extract_ollama.py
|   `-- scout_daily.py
|-- sql/
|   `-- schema.sql
|-- Dockerfile
|-- docker-compose.yml
|-- README.md
`-- requirements.txt
```

## Development Roadmap

The next work should build on the arXiv-to-SQLite MVP:

1. Use SQLite history for duplicate filtering and feedback-aware ranking.
2. Add Feedback Loop v2 for selected, accepted, declined, and saved-for-later papers.
3. Add a review queue that turns a selected triage summary into a ChatGPT section-by-section review.
4. Add provider adapters for ChatGPT/Codex and Gemini behind one interface.
5. Add general open-access PDF resolution beyond arXiv.
6. Add systemd scheduling, logs, and run reports.
7. Record benchmark runs and generated reports.

See [docs/roadmap.md](docs/roadmap.md) for phased delivery.

## Design Documents

- [Architecture](docs/architecture.md)
- [AI stack](docs/ai-stack.md)
- [Workflow](docs/workflow.md)
- [Benchmarking](docs/benchmarking.md)
- [Roadmap](docs/roadmap.md)
- [Decision log](docs/decision-log.md)
