# Project Paper

Project Paper is a system for discovering, curating, reviewing, and learning from research papers based on a user's evolving interests and feedback.

The repository currently contains a Python MVP called `paper_agents`. The V2 backend separates Scout, Curator, and Feedback responsibilities: Scout retrieves candidate pools, Curator scores and recommends papers, and the Feedback Agent will ingest manual ChatGPT discussion summaries into immutable SQLite history and versioned profiles.

## Overview

Project Paper is intended to become a provider-agnostic, hybrid local/cloud paper discovery and curation platform. The full runtime environment is planned to live on a late-2012 Mac mini running Ubuntu.

The first production-oriented implementation should favor simple Python, SQLite, explicit workflow stages, and systemd timers over a heavy agent framework.

## Canonical Local Checkout

For this project, the canonical local repository checkout is:

```text
/Users/vhl/workspace/paper-agent
```

Use this path for local commands, cron entries, deployment scripts, and future Codex work on Project Paper.

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

- `ScoutAgent` retrieves arXiv candidate pools, deduplicates source results, marks previously discovered papers as excluded, and persists Scout run telemetry without preference scores.
- `CuratorAgent` reads Scout candidates, current profile version, and history; scores every considered candidate; recommends up to three papers; and writes active scouting guidance for later Scout runs.
- `pipeline-daily` orchestrates the V2 Scout -> Curator -> Reviewer workflow, records workflow cycles, downloads/extracts recommended PDFs with Ollama, and stores artifacts in SQLite.
- SQLite stores canonical papers, alternate source records, Scout runs/candidates, Curator runs/evaluations/recommendations, versioned scouting guidance, immutable raw feedback tables, parse attempts, structured feedback, and profile versions.
- `bootstrap known-papers` backfills important seed papers, including the Microsoft/arXiv cloud incident LLM paper.
- `review-summary` creates a ChatGPT section-by-section Markdown review from a triage summary.
- The review queue UI reads Curator recommendations from SQLite and lets the user mark lightweight review statuses.

Still manual in this MVP:

- The user manually copies a recommended paper/link into a ChatGPT Paper Discussion conversation.
- The user manually copies ChatGPT's final discussion summary back into the future Feedback Agent flow.
- Feedback parsing and profile-version updates are supported at the repository/schema layer but are not yet wired into a product-facing CLI/UI flow.

Not implemented yet:

- Gemini or other provider adapters.
- General open-access PDF resolution beyond arXiv.
- systemd service and timer.
- Benchmark recording and generated run reports.

## MVP Workflow

```text
Scout
  -> Curator
  -> manual ChatGPT Paper Discussion
  -> manual final discussion summary handoff
  -> Feedback Agent
  -> SQLite
```

The current scheduled path covers Scout -> Curator -> recommended PDF extraction. The two ChatGPT handoffs remain intentionally manual while the MVP validates the workflow.

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

Run the arXiv Scout source check:

```bash
python3 -m paper_agents.cli scout-daily
```

Scout uses arXiv only in the MVP. It stores source candidate metadata in `data/scout/YYYY-MM-DD.jsonl` without preference scores, recommendation ranks, or final selection decisions. Ranking and recommendations belong to Curator inside `pipeline-daily`.

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

During the V2 reset window, recreate a local dev/Mini database with:

```bash
python3 -m paper_agents.cli db reset --yes
python3 -m paper_agents.cli bootstrap known-papers
```

Inspect the registry:

```bash
python3 -m paper_agents.cli db stats
python3 -m paper_agents.cli db recent-runs --limit 5
python3 -m paper_agents.cli db papers --selected --limit 10
```

Run the daily Scout-to-Curator pipeline:

```bash
python3 -m paper_agents.cli pipeline-daily --fetch 20 --keep 3
```

This creates a workflow cycle, runs Scout to retrieve and persist candidates, lets Curator evaluate every eligible candidate, stores up to three recommendations, downloads/extracts recommended PDFs, saves summaries under `data/extractions/`, records artifacts in `data/paper_agent.db`, and prints a compact review list. Previously discovered papers are retained as Scout candidate records with exclusion reasons instead of being re-recommended. For an interactive preview, use quick mode:

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

Run the local review queue UI:

```bash
python3 -m paper_agents.cli web --host 127.0.0.1 --port 8000
```

The review UI reads selected papers from SQLite, shows triage fields, links to registered artifacts, and writes feedback rows. Bind to `0.0.0.0` only on a trusted LAN. It also exposes the original paper link, supports status filters, latest/score sorting, full/condensed views, and a compact copy prompt for moving a paper into ChatGPT.

Install a simple daily 5:00 AM local-time cron job on the Mac mini:

```bash
(crontab -l 2>/dev/null; echo "0 5 * * * cd $HOME/workspace/paper-agent && mkdir -p logs && scripts/nightly_pipeline.sh >> logs/pipeline-daily.log 2>&1") | crontab -
```


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
|   |-- bootstrap.py
|   |-- curator.py
|   |-- curator_agent.py
|   |-- db.py
|   |-- feedback.py
|   |-- local_extract.py
|   |-- openai_helpers.py
|   |-- pipeline.py
|   |-- review.py
|   |-- reviewer_agent.py
|   |-- scout.py
|   |-- scout_agent.py
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

The next work should build on the V2 Scout/Curator backend:

1. Wire the Feedback Agent CLI/UI flow for immutable raw discussion summaries, parse attempts, structured feedback, and profile-version updates.
2. Update the review queue UI around Curator recommendations and manual ChatGPT handoff status.
3. Add provider adapters for ChatGPT/Codex and Gemini behind one source interface.
4. Add general open-access PDF resolution beyond arXiv.
5. Add systemd scheduling, logs, and run reports.
6. Record benchmark runs and generated reports.

See [docs/roadmap.md](docs/roadmap.md) for phased delivery.

## Design Documents

- [Architecture](docs/architecture.md)
- [AI stack](docs/ai-stack.md)
- [Workflow](docs/workflow.md)
- [Benchmarking](docs/benchmarking.md)
- [Roadmap](docs/roadmap.md)
- [Decision log](docs/decision-log.md)
