# Project Paper

Project Paper is a system for discovering, curating, reviewing, and learning from research papers based on a user's evolving interests and feedback.

The repository currently contains a Python MVP called `paper_agents`. The V2 backend separates Scout, Curator, and Feedback responsibilities: Scout retrieves candidate pools, Curator scores and recommends papers, and the Feedback Agent stores pasted ChatGPT discussion summaries in immutable SQLite history with deterministic v1 parsing.

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
- SQLite stores canonical papers, alternate source records, Scout runs/candidates, Curator runs/evaluations/recommendations, versioned scouting guidance, immutable raw feedback tables, parse attempts, structured feedback, applied-feedback tracking, and profile versions.
- `bootstrap known-papers` backfills important seed papers, including the Microsoft/arXiv cloud incident LLM paper.
- `review-summary` creates a ChatGPT section-by-section Markdown review from a triage summary.
- The review queue UI reads Curator recommendations from SQLite, lets the user mark lightweight review statuses, and stores pasted feedback blobs in the V2 feedback tables.
- `feedback apply` uses Gemini to synthesize unapplied structured feedback into a new active profile version after a recommended dry run.

Still manual in this MVP:

- The user manually copies a recommended paper/link into a ChatGPT Paper Discussion conversation.
- The user manually copies ChatGPT's final discussion summary back into Project Paper.
- Feedback profile evolution now auto-applies Gemini updates after Review Queue feedback submit, while keeping manual CLI controls for testing and operations.

Not implemented yet:

- Provider adapters beyond the Gemini profile-update path.
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

Store a pasted ChatGPT discussion blob in the V2 feedback tables without updating the profile:

```bash
python3 -m paper_agents.cli feedback add --paper-id 12 --status interested --file /tmp/feedback.txt
```

Preview and apply profile evolution from unapplied structured feedback:

```bash
python3 -m paper_agents.cli feedback apply --provider gemini --dry-run
python3 -m paper_agents.cli feedback apply --provider gemini
```

Inspect the stored preference profile:

```bash
docker compose run --rm paper-agents profile
```

You can still run locally with Python:

```bash
python3 -m paper_agents.cli run
```

Run the default arXiv Scout source check:

```bash
python3 -m paper_agents.cli scout-daily
```

Scout uses arXiv by default, including for nightly cron. Semantic Scholar and OpenAlex are opt-in source adapters selected with `--source semantic_scholar` or `--source openalex`. Scout stores source candidate metadata in `data/scout/YYYY-MM-DD.jsonl` without preference scores, recommendation ranks, or final selection decisions. Ranking and recommendations belong to Curator inside `pipeline-daily`.

Semantic Scholar accepts `SEMANTIC_SCHOLAR_API_KEY`, sent as the `x-api-key` request header. The key is approved and a direct CLI test has succeeded, but the source remains opt-in and rate-limited. Approved key guidance is 1 request per second cumulatively across endpoints, so use `--request-delay 2` or higher. OpenAlex uses its public API without a key. Its adapter narrows searches toward software/cloud/operations context, requests article-like work types, and filters obvious book/index/reference and biomedical noise before storage. OpenAlex remains outside the daily arXiv cron path; run it with the separate weekly rotating script so stable search results do not exhaust the same tiny topic pool every day.

Default Scout topics cover practical operations clusters such as AIOps, LLM/agentic operations, incident response, root-cause/failure diagnosis, observability/log/trace analysis, debugging, program repair, software maintenance, SRE, cloud operations, and production engineering. Curator also penalizes obvious physical-world incident domains such as railway, traffic/vehicular, medical/healthcare, power grid, smart grid, and transportation incidents.

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

For a gentle Semantic Scholar test, set `SEMANTIC_SCHOLAR_API_KEY` when available and keep `--request-delay` at 2 or higher:

```bash
python3 -m paper_agents.cli scout-daily \
  --source semantic_scholar \
  --topic "microservice diagnosis" \
  --fetch 3 \
  --keep 1 \
  --no-download \
  --request-delay 5 \
  --retries 4 \
  --source-timeout 90
```

For a gentle OpenAlex test:

```bash
python3 -m paper_agents.cli scout-daily \
  --source openalex \
  --topic "microservice diagnosis" \
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
python3 -m paper_agents.cli db health --days 21
python3 -m paper_agents.cli db health --days 21 --source openalex --json
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

The review UI reads selected papers from SQLite, shows triage fields, links to registered artifacts, and writes feedback rows. Bind to `0.0.0.0` only on a trusted LAN. It uses compact inbox-style controls, exposes the original paper link with a URL copy control, shows distinct source badges for arXiv/OpenAlex/Semantic Scholar/unknown sources, filters by source when multiple Scout sources are present, keeps quick status actions close to each paper, and shows a clearly labeled source abstract when local triage extraction is missing. Quick status buttons are status-only and should complete quickly; they show `Saving...` and a timeout hint if completion hangs. Only saving a non-empty Feedback blob triggers V2 feedback ingestion and Gemini profile apply. Cards label Curator ranking as `System`; when parsed user feedback includes a score, `Your score: N/5` is shown prominently and the system score is visually demoted.

The review queue links to `/health`, and `/health` links back to the review queue. The health dashboard computes DB integrity, latest cycle age/state, Scout/Curator/Reviewer funnel counts, artifact gaps, feedback/profile status, warnings, and source splits directly from raw SQLite facts rather than materialized aggregate tables. It includes lightweight charts and tables for daily candidates, eligible papers, recommendations, source breakdown, warning/empty-stage behavior, and feedback/profile activity. Use the range and source filters to distinguish "cron did not run" from "Scout ran but a source returned no eligible candidates."

Manual feedback apply and full profile rebuild remain available for testing and operations:

```bash
python3 -m paper_agents.cli feedback apply --provider gemini --dry-run
python3 -m paper_agents.cli feedback apply --provider gemini
python3 -m paper_agents.cli feedback rebuild-profile --provider gemini --dry-run
```

If Review Queue feedback saves but Gemini profile auto-apply fails, the UI shows a warning banner. The raw and structured feedback remain saved and unapplied for retry. Inspect recent apply attempts:

```bash
sqlite3 data/paper_agent.db "SELECT id, provider, status, error, profile_version_id, created_at FROM feedback_profile_apply_attempts ORDER BY id DESC LIMIT 10;"
```

Then retry manually. With no explicit model, `feedback apply` uses the Gemini CLI default first and falls back once to `gemini-3.1-flash-lite` only for quota/rate-limit failures:

```bash
python3 -m paper_agents.cli feedback apply --provider gemini --dry-run
python3 -m paper_agents.cli feedback apply --provider gemini
```

Use an explicit model when you want to bypass the default model; explicit `--model` choices are honored without automatic fallback:

```bash
python3 -m paper_agents.cli feedback apply --provider gemini --model gemini-3.1-flash-lite
```

Gemini profile updates use the Gemini CLI with a 180-second default timeout. If the CLI is slow on the Mac mini, raise it before retrying:

```bash
export PAPER_AGENT_GEMINI_TIMEOUT_SECONDS=240
python3 -m paper_agents.cli feedback apply --provider gemini
```

Failed default-model attempts and fallback success attempts are both recorded in `feedback_profile_apply_attempts`, so `/health` can show historic failed attempts even after retry success. Profile apply uses the Gemini CLI/API quota path; the Gemini app usage screen and Gemini API/AI Studio quota screen are different operational views.

Review profile quality after roughly 10 feedback items or if recommendation quality shows an obvious downward trend.

Backfill missing triage summaries for already recommended papers without re-scouting:

```bash
python3 -m paper_agents.cli review-backfill --quick
```

Back up the SQLite database with the online backup API. The script writes timestamped files under `backups/` and verifies each backup with `PRAGMA integrity_check`:

```bash
scripts/backup_db.sh
```

Recommended weekly backup cron entry on the Mac mini:

```bash
0 4 * * 0 cd $HOME/workspace/paper-agent && mkdir -p logs && scripts/backup_db.sh >> logs/backup-db.log 2>&1
```

Restore expectation: restore from a copied backup only after checking it with `PRAGMA integrity_check`; keep restore drills manual until the operating model matures.

Install log rotation for cron logs:

```bash
sudo cp deploy/project-paper.logrotate /etc/logrotate.d/project-paper
```

Tail local Project Paper logs:

```bash
scripts/tail_logs.sh
```

The current intended Mac mini pipeline crontab has three source jobs: daily arXiv at 5:00 AM, weekly Monday OpenAlex at 6:30 AM via the rotating topic script, and weekly Tuesday Semantic Scholar at 6:00 AM. The Semantic Scholar entry sources `$HOME/.bashrc` so `SEMANTIC_SCHOLAR_API_KEY` is available to cron.

```cron
0 5 * * * cd /home/devitolo/workspace/paper-agent && mkdir -p logs && scripts/nightly_pipeline.sh >> logs/pipeline-daily.log 2>&1
30 6 * * 1 cd /home/devitolo/workspace/paper-agent && mkdir -p logs && scripts/openalex_pipeline.sh >> logs/pipeline-openalex.log 2>&1
0 6 * * 2 cd $HOME/workspace/paper-agent && mkdir -p logs && . $HOME/.bashrc && python3 -m paper_agents.cli pipeline-daily --source semantic_scholar --quick --topic "microservice diagnosis" --fetch 10 --keep 2 --max-scout-attempts 1 --request-delay 2 --retries 4 --source-timeout 90 >> logs/pipeline-semantic-scholar.log 2>&1
```

Do not install the old one-off direct `pipeline-daily --source openalex ...` cron line; it is superseded by `scripts/openalex_pipeline.sh`, which rotates practical operations topics, fetches 30, keeps 2, and caps Scout attempts at one because OpenAlex search is stable for repeated queries.

Override the weekly topic for a one-off run:

```bash
PAPER_AGENT_OPENALEX_TOPIC="AIOps root cause analysis cloud incidents" scripts/openalex_pipeline.sh
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
|   |-- product-changelog.md
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

1. Review profile quality after 10 feedback items, or earlier if recommendation quality clearly declines.
2. Consider caching reviewed Gemini dry-run proposals so apply does not spend quota on the same synthesis twice.
3. Improve deterministic feedback parsing or replace it with a model-backed parser once enough real blobs exist.
4. Add provider adapters beyond Gemini for profile synthesis.
5. Add general open-access PDF resolution beyond arXiv.

See [docs/roadmap.md](docs/roadmap.md) for phased delivery.

## Design Documents

- [Architecture](docs/architecture.md)
- [AI stack](docs/ai-stack.md)
- [Workflow](docs/workflow.md)
- [Benchmarking](docs/benchmarking.md)
- [Roadmap](docs/roadmap.md)
- [Decision log](docs/decision-log.md)
- [Product changelog](docs/product-changelog.md)
