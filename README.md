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
- `scout-daily` runs a deterministic arXiv Scout MVP with keyword ranking, JSONL metadata storage, top-five selection, and PDF downloads for selected papers.
- OpenAI scores candidate titles and abstracts in the older `run` prototype.
- `ResearchCurator` selects a short reading list from the scout output.
- `FeedbackAgent` updates `data/profile.json` from natural-language feedback.
- Docker can run the CLI with a mounted `.env` and `data/` directory.

Not implemented yet:

- SQLite registry or feedback database.
- Gemini or other provider adapters.
- General open-access PDF resolution beyond arXiv.
- PDF text or section extraction.
- Local model inference.
- Duplicate/history filtering beyond the preference profile.
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

The current prototype implements only a thin arXiv plus OpenAI version of the scout, curator, and feedback loop.

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

The first Scout implementation uses arXiv only, stores all candidate metadata in `data/scout/YYYY-MM-DD.jsonl`, ranks candidates with deterministic keywords, keeps the top 5, and downloads PDFs for the selected papers into `data/papers/arxiv/`.

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
|   |-- feedback.py
|   |-- openai_helpers.py
|   |-- scout.py
|   `-- store.py
|-- scripts/
|   `-- paper_extract_ollama.py
|-- Dockerfile
|-- docker-compose.yml
|-- README.md
`-- requirements.txt
```

## Development Roadmap

The next work should move from the prototype toward explicit data models and persistence:

1. Define common paper, provider, run, and feedback records.
2. Add SQLite persistence for paper history, user feedback, and telemetry.
3. Add a manual-input proof of concept for paper links and PDFs.
4. Benchmark local model candidates on Project Paper tasks.
5. Add provider adapters for ChatGPT/Codex and Gemini behind one interface.
6. Add open-access PDF resolution, download, hashing, and extraction.
7. Add systemd scheduling, logs, and run reports.

See [docs/roadmap.md](docs/roadmap.md) for phased delivery.

## Design Documents

- [Architecture](docs/architecture.md)
- [AI stack](docs/ai-stack.md)
- [Workflow](docs/workflow.md)
- [Benchmarking](docs/benchmarking.md)
- [Roadmap](docs/roadmap.md)
- [Decision log](docs/decision-log.md)
