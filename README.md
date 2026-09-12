# Project Paper

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

Project Paper is a local-first system for discovering, curating, reviewing, and learning from research papers based on a user's evolving interests and feedback.

> **First supported release:** the installer-fronted Docker Compose path and manual in-product Scout have passed exact-image acceptance on Ubuntu x86-64. The public image also includes arm64; macOS Apple Silicon remains a preview path pending a clean exact-release end-to-end pass. See the [package usage guide](docs/m1-package-usage.md), [acceptance report](docs/m1-qa-report.md), and [productization plan](docs/productization-plan.md).

The repository currently contains a Python MVP called `paper_agents`. The V2 backend separates Scout, Curator, and Feedback responsibilities: Scout retrieves candidate pools, Curator scores and recommends papers, and the Feedback Agent stores pasted ChatGPT discussion summaries in immutable SQLite history with deterministic v1 parsing.

## First Supported Path

The current first-user path is:

1. Use a fresh clone on Linux x86-64 with Docker Engine and Compose v2.
2. Run the installer; its defaults select the public Project Paper image and pinned Ollama image.
3. Open the loopback web UI.
4. Add an enabled arXiv topic in **Topics**.
5. Return to **Review Queue** and choose **Run Scout**.
6. Review returned papers, copy a discussion prompt to ChatGPT, then paste the final feedback blob back into Project Paper.

From a clean checkout:

```bash
cd /absolute/path/to/paper-agent
bash scripts/install_project_paper.sh
```

The installer creates `.env` if missing, selects the immutable `v0.1.2` image digest, prepares the default local Qwen model in Docker, starts the app on a loopback URL, and prints runtime/log/retry commands. Docker selects the matching arm64 image automatically. The packaged path requires Docker with Compose; it does not require host Python, host Ollama, cron, systemd, OpenAI, Gemini, OpenAlex, or Semantic Scholar credentials for the default arXiv flow.

Gemini profile synthesis is disabled in this package. Feedback blobs are still saved, parsed when possible, and available to deterministic Scout guidance, but direct profile evolution is not promised without the later optional Gemini packaging work.

The packaged runtime stores user state in Compose volumes: `paper-data` for SQLite/artifacts/profile, `paper-config` for topics, and `ollama-data` for the model cache. Re-run the installer to repair/retry without deleting volumes. Do not use `db reset`, remove Compose volumes, or change the recorded project name as a normal recovery or upgrade step.

macOS exact-release qualification, release upgrade/rollback, and broader backup/restore guarantees remain tracked work. Support is through GitHub Issues for the current major version and one prior major version.

## Overview

Project Paper is intended to become a provider-agnostic, hybrid local/cloud paper discovery and curation platform. The full runtime environment is planned to live on a late-2012 Mac mini running Ubuntu.

The first production-oriented implementation should favor simple Python, SQLite, explicit workflow stages, and systemd timers over a heavy agent framework.

## Local Checkout

Clone Project Paper into a directory you control. The native Mini examples use:

```text
$HOME/workspace/paper-agent
```

Override `PAPER_AGENT_REPO` when using another native checkout location. The packaged installer works from the current clone and records its installation directory.

## Architecture Summary

- The Mac mini is the runtime host.
- Scheduled workflows run locally on Ubuntu.
- Cloud AI providers are used selectively for paper scouting and higher-value reasoning.
- Local models handle high-volume filtering, scoring, summarization, and extraction where benchmarks show they are effective.
- The workflow should support switching between ChatGPT and Gemini without rewriting downstream processing.
- Previously seen, accepted, declined, and highly rated papers are stored and reused in future filtering.
- Open-access PDFs should be downloaded automatically when available.
- Deterministic code should handle downloading, parsing, filtering, and storage; LLMs should make judgments rather than perform file transfer.

See [docs/architecture.md](docs/architecture.md) and [docs/ai-stack.md](docs/ai-stack.md) for the target design. The Mac mini operational notes later in this README describe the creator's native deployment and are not the new-user packaged install path.

## Current Project Status

Implemented today:

- `ScoutAgent` retrieves candidate pools, expands source queries with deterministic feedback/profile guidance, deduplicates source results, marks previously discovered or clear feedback-avoid matches as excluded, and persists Scout run telemetry without preference scores.
- `CuratorAgent` reads Scout candidates, current profile version, history, and stored artifact provenance; computes deterministic relevance/profile fit; runs a bounded local Qwen evidence assessment per candidate; recommends up to three papers; and writes active scouting guidance for later Scout runs.
- `pipeline-daily` orchestrates the V2 Scout -> Curator -> Reviewer workflow, records workflow cycles, downloads/extracts recommended PDFs with Ollama, and stores artifacts in SQLite.
- SQLite stores canonical papers, alternate source records, Scout runs/candidates, Curator runs/evaluations/recommendations, versioned scouting guidance, immutable raw feedback tables, parse attempts, structured feedback, applied-feedback tracking, and profile versions.
- `bootstrap known-papers` backfills important seed papers, including the Microsoft/arXiv cloud incident LLM paper.
- `review-summary` creates a ChatGPT section-by-section Markdown review from a triage summary.
- The review queue UI reads Curator recommendations from SQLite, opens/copies paper context for external discussion, and stores pasted feedback blobs in the V2 feedback tables.
- Review Queue feedback save queues Gemini profile synthesis automatically; `feedback apply` remains available for manual dry-run/apply recovery.

Still manual in this MVP:

- The user manually copies a recommended paper/link into a ChatGPT Paper Discussion conversation.
- The user manually copies ChatGPT's final discussion summary back into Project Paper.
- Feedback profile evolution auto-applies Gemini updates after Review Queue feedback submit, while keeping manual CLI controls for testing and operations.

Not implemented yet:

- Provider adapters beyond the Gemini profile-update path.
- General open-access PDF resolution beyond arXiv, beyond the current Semantic Reader fallback.
- systemd service and timer.
- Benchmark recording and generated run reports.
- macOS Apple Silicon exact-release end-to-end qualification.
- A supported packaged upgrade/rollback path.

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

Supported packaged path:

- Linux x86-64 with Docker Engine and Compose v2. macOS Apple Silicon with Docker Desktop is available as a preview.
- Network access to arXiv and to pull Docker/Ollama images/model weights.
- Docker configured with at least the installer's provisional 4 GiB memory and 6 GiB free-space preflight thresholds.

Current native prototype/Mini operations:

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

These are historical native/Mini operations, not packaged onboarding. The current Compose file has no `paper-agents` service, so the legacy `docker compose run --rm paper-agents ...` examples below do not work with this package. Use the [M1 guide](docs/m1-package-usage.md) for installation, review, diagnostics and recovery; native CLI examples require their own host dependencies and configuration.

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

Scout uses arXiv by default, including for nightly cron. Semantic Scholar and OpenAlex are opt-in source adapters selected with `--source semantic_scholar` or `--source openalex`. Scout V2 reads the active profile plus recent structured feedback to build deterministic guidance: high-scored keep feedback can add a small bounded set of boost terms to source queries, while low-scored reject feedback contributes avoid terms for conservative pre-Curator filtering. Scout records the guidance summary in `scouting_guidance` and `scout_runs.diagnostics_json`, and source candidate diagnostics keep softer boost/avoid matches visible. Scout stores source candidate metadata in `data/scout/YYYY-MM-DD.jsonl` without preference scores, recommendation ranks, or final selection decisions. Ranking and recommendations still belong to Curator inside `pipeline-daily`. Deep paper/PDF reading remains out of scope for Scout and is future Curator V2 evidence-aware reranking work.

Semantic Scholar accepts `SEMANTIC_SCHOLAR_API_KEY`, sent as the `x-api-key` request header. The key is approved and a direct CLI test has succeeded, but the source remains opt-in and rate-limited. Approved key guidance is 1 request per second cumulatively across endpoints, so use `--request-delay 2` or higher. OpenAlex uses its public API without a key. Its adapter narrows searches toward software/cloud/operations context, requests article-like work types, and filters obvious book/index/reference and biomedical noise before storage. OpenAlex remains outside the daily arXiv cron path; run it with the separate weekly rotating script so stable search results do not exhaust the same tiny topic pool every day.

Scout topics live in `config/topics.yaml` and can be steered from `/topics`, the Topic Management page in the Review Queue web server. The default flow is TopicAgent: describe what you want Project Paper to scout or change, local Ollama/Qwen proposes structured topic config, and you approve before anything is saved. The default model is `qwen2.5:1.5b-instruct`, configurable with `PAPER_AGENT_TOPIC_MODEL`; the Ollama generate URL can be changed with `PAPER_AGENT_TOPIC_OLLAMA_URL` or `PAPER_AGENT_OLLAMA_URL`; timeout uses `PAPER_AGENT_TOPIC_TIMEOUT_SECONDS`, default 30 seconds. `remove`/`delete`/`drop` physically remove a topic from `config/topics.yaml`; `disable`/`turn off`/`pause` preserves it with `enabled: false`. Scheduled source jobs select enabled config topics on future runs, while explicit CLI `--topic` values still override the config for one run. Saving topics does not run Scout immediately; the packaged Review Queue has a separate **Run Scout** action. Curator also penalizes obvious physical-world incident domains such as railway, traffic/vehicular, medical/healthcare, power grid, smart grid, and transportation incidents.

Feedback blobs can include `Score: N` or decimal ratings such as `Score: 4.5`; parsed user scores must be between 1 and 5 and display on Review Queue cards as `Your score: N/5`.

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

This creates a workflow cycle, runs Scout to retrieve and persist candidates, lets Curator evaluate every eligible candidate with deterministic relevance/profile scoring plus bounded local Qwen evidence assessment, stores up to three recommendations, downloads/extracts recommended PDFs, saves summaries under `data/extractions/`, records artifacts in `data/paper_agent.db`, and prints a compact review list. Curator V3 evidence assessment uses local Qwen sequentially, defaulting to 45 seconds and 7000 input characters per candidate. It uses full-text triage only when a readable triage artifact is available; otherwise it uses the source abstract or metadata. Assessment, provenance, model, timing, and score components are persisted in `curator_runs.metadata_json`. Previously discovered papers, papers already recommended before the current Scout run, and clear feedback-avoid matches are retained as Scout candidate records with exclusion reasons instead of being re-recommended. Bounded rescout attempts fetch a deeper candidate window so stale top results do not repeatedly satisfy the daily quota. The command output/logs include a `Scout guidance:` line when feedback/profile guidance is available. For an interactive preview, use quick mode:

```bash
python3 -m paper_agents.cli pipeline-daily \
  --quick \
  --fetch 20 \
  --keep 3 \
  --request-delay 5 \
  --retries 4 \
  --source-timeout 90
```

For a small Mini smoke test of feedback-guided retrieval against OpenAlex:

```bash
cd ~/workspace/paper-agent
git pull origin main
python3 -m paper_agents.cli pipeline-daily \
  --source openalex \
  --topic "AIOps root cause analysis cloud incidents" \
  --quick \
  --fetch 5 \
  --keep 1 \
  --max-scout-attempts 1 \
  --request-delay 5 \
  --retries 4 \
  --source-timeout 90
```

Expected output includes `Scout guidance:` with boost/avoid terms when enough profile or feedback signals exist.

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

The review UI reads selected papers from SQLite, shows triage fields including clearly labeled abstract-only fallbacks, keeps raw summary JSON out of regular cards, and writes feedback rows. Bind to `0.0.0.0` only on a trusted LAN. It uses compact inbox-style controls with navigation separated from unlabeled filter controls, defaults to Highest score, opens the paper from the title, keeps Open PDF and Copy next to source/date/source-id metadata, shows distinct source badges for arXiv/OpenAlex/Semantic Scholar/unknown sources, filters by source when multiple Scout sources are present, and shows a clearly labeled source abstract when local triage extraction is missing. Primary filters display as All papers, Scored, and Needs review. `Scored` means user feedback-backed papers with saved raw or structured feedback, not merely a legacy reviewed status; it can include papers outside the current recommendation queue. Historical lightweight statuses such as interested, read_later, not_interested, and reviewed remain compatible with older CLI/parser paths, but they are backend compatibility only and can be removed from the legacy feedback table with `python3 -m paper_agents.cli db cleanup-legacy-feedback --yes`. Feedback is hidden behind Add feedback or View/edit feedback until needed. Scored papers reopen View/edit feedback with the latest saved `raw_feedback.content` blob prefilled, falling back to legacy `feedback.notes` only for older records; no migration is needed after `git pull` and web restart. `Copy discussion prompt` copies a Paper Discussion handoff prompt built from the current card data, including links, scores, rationale, signals, and local triage/source abstract context; paste its final feedback blob back into the same Feedback box. Cards label Curator ranking as `Match Score`, matched keyword chips as `Signals`, and personalized Curator rationale as `Why this matches you`; simple keyword-echo rationales omit extra text when signal pills are present while richer Curator prose remains visible. When parsed user feedback includes an integer or decimal score, `Your score: N/5` is shown with the same numeric size as the match score.

The review queue links to `/health`, and `/health` links back to the review queue. The health dashboard computes DB integrity, latest cycle age/state, Scout/Curator/Reviewer funnel counts, artifact gaps, feedback/profile status, warnings, and source splits directly from raw SQLite facts rather than materialized aggregate tables. It includes lightweight charts and tables for daily candidates, eligible papers, recommendations, source breakdown, warning/empty-stage behavior, and feedback/profile activity. Warnings are meant to represent actionable work, and the warning section is hidden entirely when there are no warnings. When `/health` does show warnings, start with `scripts/health_warnings.sh`; it prints the normal health report plus detailed rows behind common warnings. Use the range and source filters to distinguish "cron did not run" from "Scout ran but a source returned no eligible candidates."

Mini runbook helpers for common health warnings:

```bash
scripts/health_warnings.sh
scripts/fix_missing_triage_summaries.sh --quick --limit 5
scripts/apply_pending_feedback_profile.sh --dry-run
scripts/apply_pending_feedback_profile.sh --apply
```

`fix_missing_triage_summaries.sh` backfills summaries for recommendations with PDFs, or creates an abstract-only triage summary when no PDF can be downloaded but source metadata includes an abstract, and does not rerun Scout or Curator. `apply_pending_feedback_profile.sh --dry-run` previews pending profile application; `--apply` writes the profile update and a later successful non-dry-run apply clears the Gemini/profile warning. DOI-only Semantic Scholar/OpenAlex records without a PDF artifact do not create the missing-summary repair warning, and they do not render an `Open PDF` action. Semantic Scholar backfill also tries `/reader/{paperId}` and follows the reader download link when it resolves to real PDF content; if that still fails and a source abstract exists, the fallback artifact is marked abstract-only. Recommended Semantic Scholar/OpenAlex records can remain visible from source metadata so queue volume is not silently lost. Database changes show up on browser refresh; code, template, and static asset changes from `git pull` require restarting the long-running Python web server. Replaced static assets may also need a hard browser refresh if cached.

Manual feedback apply and full profile rebuild remain available for testing and operations:

```bash
python3 -m paper_agents.cli feedback apply --provider gemini --dry-run
python3 -m paper_agents.cli feedback apply --provider gemini
python3 -m paper_agents.cli feedback rebuild-profile --provider gemini --dry-run
scripts/compare_feedback_profile_rebuild.sh
scripts/biweekly_profile_rebuild_compare.sh
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

Review profile quality after roughly 10 feedback items or if recommendation quality shows an obvious downward trend. The biweekly compare wrapper is safe for cron because it only runs the Gemini rebuild in dry-run comparison mode, writes no active profile update, and skips Mondays outside the two-week cadence anchored at `2026-09-07`. Override the cadence anchor with `PAPER_AGENT_PROFILE_REBUILD_ANCHOR=YYYY-MM-DD` if the schedule needs to move.

Backfill missing triage summaries for already recommended papers without re-scouting. This extracts PDFs when available and may create an abstract-only triage artifact from source metadata when no PDF can be downloaded:

```bash
python3 -m paper_agents.cli review-backfill --quick
```

Back up the SQLite database with the online backup API. The script writes timestamped files under `backups/` and verifies each backup with `PRAGMA integrity_check`:

```bash
scripts/backup_db.sh
```

Recommended weekly backup cron entry on the Mac mini:

```bash
0 1 * * 0 cd "$HOME/workspace/paper-agent" && mkdir -p logs && scripts/backup_db.sh >> logs/backup-db.log 2>&1
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

The current intended Mac mini pipeline crontab has OpenAlex at 4:00 AM, arXiv at 5:00 AM, Semantic Scholar at 6:00 AM, a biweekly Monday 2:00 AM Gemini profile rebuild comparison, and a Sunday 1:00 AM SQLite backup. The Semantic Scholar and profile comparison entries source `$HOME/.bashrc` so API/provider environment is available to cron. Each job uses a distinct non-blocking `flock` file under `/tmp` (override with `PAPER_AGENT_LOCK_DIR`) and exits successfully with a clear log message when a prior run is still active.

Cron jobs run the currently deployed checkout; they do not pull code while running. Deploy updates explicitly, then refresh the managed crontab if its template changed:

```bash
git pull --ff-only
scripts/install_project_paper_cron.sh --apply
```

Install or refresh the repo-owned Mac mini crontab after `git pull`:

```bash
scripts/install_project_paper_cron.sh --dry-run
scripts/install_project_paper_cron.sh --apply
```

The installer preserves unrelated cron entries, removes older Project Paper cron lines, and installs the managed block from `deploy/project-paper.crontab`.

```cron
# Daily OpenAlex Scout/Curator/Reviewer pipeline with rotating configured topics.
0 4 * * * cd "$HOME/workspace/paper-agent" && mkdir -p logs && scripts/openalex_pipeline.sh >> logs/pipeline-openalex.log 2>&1
# Daily arXiv Scout/Curator/Reviewer pipeline.
0 5 * * * cd "$HOME/workspace/paper-agent" && mkdir -p logs && scripts/nightly_pipeline.sh >> logs/pipeline-daily.log 2>&1
# Biweekly Monday Gemini full feedback-profile rebuild comparison; dry-run only, never applies.
0 2 * * 1 . "$HOME/.bashrc" && cd "$HOME/workspace/paper-agent" && mkdir -p logs && scripts/biweekly_profile_rebuild_compare.sh >> logs/profile-rebuild-compare.log 2>&1
# Daily Semantic Scholar Scout/Curator/Reviewer pipeline; sources API key from bashrc.
0 6 * * * . "$HOME/.bashrc" && cd "$HOME/workspace/paper-agent" && mkdir -p logs && scripts/semantic_scholar_pipeline.sh >> logs/pipeline-semantic-scholar.log 2>&1
# Weekly SQLite backup with integrity check.
0 1 * * 0 cd "$HOME/workspace/paper-agent" && mkdir -p logs && scripts/backup_db.sh >> logs/backup-db.log 2>&1
```

Do not install the old one-off direct `pipeline-daily --source openalex ...` cron line; it is superseded by `scripts/openalex_pipeline.sh`, which rotates enabled `config/topics.yaml` OpenAlex topics, fetches 30, keeps 2, and caps Scout attempts at one because OpenAlex search is stable for repeated queries. The Semantic Scholar cron omits `--topic` so it also reads the editable topic config.

Override the OpenAlex topic for a one-off run:

```bash
PAPER_AGENT_OPENALEX_TOPIC="AIOps root cause analysis cloud incidents" scripts/openalex_pipeline.sh
```

Run the Review Queue web UI under systemd user supervision on the Mini:

```bash
mkdir -p ~/.config/systemd/user ~/.config/project-paper
cp deploy/systemd/project-paper-web.service ~/.config/systemd/user/
# Optional: put API/provider variables in ~/.config/project-paper/project-paper.env
systemctl --user daemon-reload
systemctl --user enable --now project-paper-web.service
loginctl enable-linger "$USER"  # optional: keep it running after logout/reboot
systemctl --user status project-paper-web.service
journalctl --user -u project-paper-web.service -f
```

After a code update, restart only the web UI with `systemctl --user restart project-paper-web.service`.


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
2. Observe Curator V3 local Qwen quality and latency on the Mini before claiming measured preference or throughput gains.
3. Consider caching reviewed Gemini dry-run proposals so apply does not spend quota on the same synthesis twice.
4. Improve deterministic feedback parsing or replace it with a model-backed parser once enough real blobs exist.
5. Add provider adapters beyond Gemini for profile synthesis.
6. Add general open-access PDF resolution beyond arXiv.

See [docs/roadmap.md](docs/roadmap.md) and [docs/productization-plan.md](docs/productization-plan.md) for phased delivery.

## Design Documents

- [Contributing](CONTRIBUTING.md) — scope, privacy rules, and validation steps for changes.
- [Security policy](SECURITY.md) — supported versions and private vulnerability reporting.
- [M1 package usage](docs/m1-package-usage.md) — current development Compose installer and first manual discovery path.
- [M1 acceptance report](docs/m1-qa-report.md) — macOS Apple Silicon evidence and remaining release gates.
- [First supported release productization plan](docs/productization-plan.md) — PM-aligned Compose assessment, Fresh Install Produces Papers milestone, release gates, and owner-assigned backlog.
- [Architecture](docs/architecture.md)
- [AI stack](docs/ai-stack.md)
- [Workflow](docs/workflow.md)
- [Benchmarking](docs/benchmarking.md)
- [Roadmap](docs/roadmap.md)
- [Decision log](docs/decision-log.md)
- [Product changelog](docs/product-changelog.md)

## License

Project Paper is licensed under the [Apache License 2.0](LICENSE). See [NOTICE](NOTICE) for the copyright notice.

### Scout warning diagnostics

Health warnings for arXiv, OpenAlex, and Semantic Scholar include **Download diagnostics** for the exact Scout run. Scheduled pipelines automatically save a local JSON snapshot when Scout returns a small or empty eligible pool or source errors, and refresh it after Curator to include evaluation/run context and recommendation counts. Capture uses the local Python diagnostic collector; it does not repeat source requests or call a model.

Reports are stored in the application database (`scout_diagnostic_reports`) and retained with their Scout run. They include run time and capture time, source/configured topics, source/refill diagnostics, errors, candidate counts, exclusion reasons, and candidate titles/queries. They do not collect `.env`, system logs, or credentials from the environment. Downloads for older runs reconstruct a report from recorded database rows and use `capture_phase: requested`; automatic snapshots use `scout_complete` or `curator_complete`.

The same collector can be run manually:

```bash
python3 -m paper_agents.scout_diagnostics --db data/paper_agent.db --run-id 123
# Or inspect the latest recorded run for a source:
scripts/diagnose_scout_run.sh openalex
```

The table is added by normal database initialization after updating the app. No new cron job is required. Diagnostic capture failures are logged without stopping Scout or Curator; the download can still reconstruct the recorded evidence.
