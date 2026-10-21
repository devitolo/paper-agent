# Mini production container operations

## Pipeline-role deployment and recovery

For the UI/pipeline separation release, use the
[runtime-role procedure](mini-runtime-roles.md). Production keeps the `app`
service for cron but runs `PAPER_AGENT_RUNTIME_ROLE=pipeline`, without a web
listener or published port. Use `python -m paper_agents.package_runtime
check-pipeline` inside that container for local readiness; do not request HTTP.
The image identity for an actual cutover comes from its main Actions evidence.

`scripts/mini_production_update.sh` and its generated recovery scripts are
**unsupported for the pipeline-role release**: they expect HTTP and rewrite cron.
Do not use the legacy updater instructions below for a pipeline container.
The supported operator path fences/drains jobs, takes a consistent backup,
preserves the existing env/override/image for rollback, and changes only the
image, role, removed port and healthcheck in the override already named by cron.
Verify local readiness, absent listener/port, unchanged state and static cron
command compatibility, then release fences. On failure, restore the retained
image/env/override (including its former role/port) against current data, without
restoring a stale DB or rewriting cron. AWS and sync are outside this operation.

The snapshot and HTTP examples below describe the earlier combined UI/pipeline
runtime. They remain applicable only when explicitly rolling back to that role.

This runbook records the production state established on 2026-09-24. It is for
the Project Paper Mini operator, not the public first-user package.

## Current runtime

| Component | Production state |
|---|---|
| App | `paper-mini-production-app-1`, `ghcr.io/devitolo/paper-agent@sha256:bbbe83615264947497b9dea0a5099e1c98cec07710c3fb083dc64347134d6204`, healthy, published as `127.0.0.1:8000->8000/tcp` |
| MiniLM | `paper-minilm-production-minilm-1`, `ghcr.io/devitolo/paper-agent-minilm@sha256:6015647271e23c9e667c8f7c50440d2330a36ef780578990b4c4f932fd08ff1b`, healthy, private internal API only |
| Ollama | `paper-mini-production-ollama-1`, image `sha256:0ab10b9b9dc5f50d30dc61aec25e3316822ca22cf0f27d4e98d74cc7dedd7c80`, running |
| Web systemd unit | Native `project-paper-web.service` inactive |
| Ollama systemd unit | Native `ollama.service` inactive |
| Scheduler | Host crontab |
| Job execution | `scripts/mini_container_job.sh` enters the app container and runs the existing workflow wrapper |

The web UI remains host-loopback-only. Containerizing execution did not move
scheduling into the app and did not make Project Paper a public service.

## Routine checks

Use the live host crontab as the schedule source of truth during the soak:

```sh
crontab -l
```

Confirm the app, standalone MiniLM service, and Ollama containers, plus loopback
publication:

```sh
docker ps --filter name=paper-mini-production
docker inspect --format '{{.Name}} {{.Image}} {{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' \
  paper-mini-production-app-1 paper-minilm-production-minilm-1 paper-mini-production-ollama-1
curl --fail --silent --show-error http://127.0.0.1:8000/ >/dev/null
```

Check that the native services remain inactive rather than starting a second
writer or model server:

```sh
systemctl --user is-active project-paper-web.service
systemctl is-active ollama.service
```

The expected answer for both native services is `inactive`. Do not re-enable
them while the container runtime is authoritative.

## Scheduled jobs

Host cron retains the existing times and log redirection. Each production entry
calls one of these launcher jobs:

| Job | Launcher argument | Schedule |
|---|---|---|
| OpenAlex pipeline | `openalex` | daily 04:00 |
| arXiv pipeline | `arxiv` | daily 05:00 |
| Semantic Scholar pipeline | `semantic` | daily 06:00 |
| CORE pipeline | `core` | daily trial source run; see live crontab |
| ZenML industry discovery | `zenml` | Sunday 23:30 America/Los_Angeles |
| Gemini profile comparison | `profile` | biweekly cadence checked by the Monday 02:00 wrapper; dry-run only |
| SQLite backup | `backup` | Sunday 01:00 |

The launcher executes the corresponding wrapper inside the app container and
holds the shared runtime lifecycle lease across preflight, writes, descendant
cleanup, and completion. Cron remains the only scheduling authority; the parked
container scheduler must remain disabled.

The committed `deploy/project-paper.crontab` uses the container launcher. The
installer refuses templates that still contain native `.venv` pipeline commands.
Inspect the live schedule with `crontab -l` before and after any change.

CORE is the fourth scheduled source under trial. It uses `CORE_API_KEY` from the
production env file and should authenticate with `Authorization: Bearer
[CORE_API_KEY]`; never put the real key in command lines, shell history, logs,
docs, or chat. Register keys at <https://core.ac.uk/services/api>. For this
private single-user Project Paper deployment, the Personal profile is usually
appropriate unless the key is genuinely requested through an academic or
institutional affiliation.

The CORE adapter uses API v3 Works search as the discovery path because Works
are deduplicated/enriched scholarly records. Do not switch the scheduled job to
Outputs as the first discovery path; Outputs are raw harvested source records.
Keep request budgets conservative, use roughly 3-second spacing, honor
`X-RateLimit-Limit`, `X-RateLimit-Remaining`, and
`X-RateLimit-Retry-After`, and cool down immediately on HTTP 429. CORE's
published limits are 100 tokens/day and 10 requests/minute unauthenticated, or
1,000 tokens/day and 25 requests/minute for registered Personal users; simple
queries usually cost 1 token and complex queries can cost 3-5.

CORE is metadata-first in Project Paper. The first pass stores records and
links, not bulk PDFs. If later selected-paper enrichment uses CORE full text,
prefer the source-provided `downloadUrl`; do not bypass the CORE API/fileserver
or systematically harvest PDFs. The current production launcher is
`scripts/mini_container_job.sh core`, logs to `logs/pipeline-core.log`, runs
the normal Curator path with MiniLM interest fit and Qwen3 evidence assessment.
The legacy MiniLM Eval/shadow experiment pages are not the production quality
path.

ZenML industry discovery is the weekly non-academic lane. The committed cron
template runs:

```sh
30 23 * * 0 cd "$HOME/paper-mini-rehearsal/candidate" && mkdir -p logs && bash scripts/mini_container_job.sh zenml >> logs/pipeline-zenml.log 2>&1
```

`scripts/mini_container_job.sh zenml` enters the app container and runs
`python -m paper_agents.migration_job zenml`, which delegates to
`scripts/zenml_pipeline.sh`. The pipeline has its own nonblocking
`project-paper-zenml.lock`; an overlap prints a skip message and exits
successfully. The ZenML import itself reads `config/industry.json`, honors the
enable flag and `max_items_per_run`, stores artifacts under `data/zenml-pilot`,
and does not run academic MiniLM relevance.

Operators can preview or run the import inside the app context with:

```sh
python -m paper_agents.cli zenml-pilot --dry-run
python -m paper_agents.cli zenml-pilot
```

Use `/topics?section=industry` to enable/disable the weekly lane or adjust
preferred/skip signals. Production deployment preserves mounted config; do not
replace `config/industry.json` with defaults during rollout.

Deployment verification checks that exactly one active ZenML cron entry exists.
Light deploy validation records `zenml_deploy_check=light`; full validation
adds a bounded `zenml-pilot --dry-run` smoke check. If post-deploy verification
reports `zenml_weekly_cron=invalid`, inspect `crontab -l` for missing or
duplicate `scripts/mini_container_job.sh zenml` entries before changing code.

## Releases

Use [Mini self-hosted CI/CD](mini-self-hosted-cicd.md) for the preferred
application deployment path. Use [Mini release runbook](mini-release-runbook.md)
for manual fallback. Both paths are image-based: build and accept a
`mini-production` image from a committed revision, then pull that immutable
digest on the Mini and recreate the existing `app` service. Do not deploy
application code by `git pull`, and do not rebuild on production.

SSH is not the default operating path. Use the
[Mini self-hosted CI/CD SSH access policy](mini-self-hosted-cicd.md#ssh-access-policy)
and get explicit operator approval before opening a Mini shell for production
work.

## Read-only health signal

The **Mini Health Snapshot** GitHub Actions workflow is the first SRE signal
for production problems. It runs on pushes to `mini-production`, manually from
the Actions tab, and on a daily schedule on the `project-paper-mini`
self-hosted runner. The workflow enters the already-running app container with
`python -m paper_agents.cli db health --json`, writes the status,
warnings/criticals, latest source runs, funnel counts, recent job events,
diagnostic run IDs, and the trend summary to both the Actions log and step
summary, and uploads the raw JSON snapshot plus a JSONL trend file for 14 days.
It is read-only: it does not restart containers, run migrations, change the
database, alter cron, or deploy.

Configure the runner/repository variable `PAPER_MINI_ENV_FILE` with the
absolute path to the private production env file. The workflow passes that
path to Docker Compose for interpolation; it never sources or prints the file.
The local equivalent is:

```sh
PAPER_MINI_ENV_FILE=/absolute/path/to/production.env \
  scripts/mini_health_snapshot.sh
```

Warnings and criticals intentionally make the workflow fail after preserving the
summary and artifact. A failed Mini Health Snapshot means "attention required,"
not that the health collection itself necessarily broke. The snapshot includes
source run IDs, provider/cooldown details already exposed by the health report,
and profile maintenance notices. The formatter accepts both the older mapping
shape and the current list-shaped `profile_maintenance` notices so maintenance
warnings remain readable in the log. SSH remains an explicit-approval path for
follow-up investigation only.

Each artifact also contains `health-trend.jsonl`. It has one stable record per
warning or collection failure with the capture time, normalized category,
source, run ID, candidate/eligible/recommendation counts, severity, and origin
(`production_health` versus `workflow_plumbing`). SRE can compare the last 7 or
30 workflow runs with `gh run list` and `gh run download` while artifacts are
retained. No trend rows are written to production SQLite. Repeated source
warnings should be handled as a health/dashboard improvement rather than
becoming a reason for recurring SSH inspection.

The Actions summary also prints a `7-day source trends` section directly. It
uses the recent source-run rows already returned by `db health --json` and
reports, per source, how often the eligible pool was below 10, how often there
were eligible papers but zero recommendations, and how many recent runs looked
healthy. GitHub Actions retention limits this to the runs and health window
still available; it does not reconstruct missing history or infer provider
429s when the source did not record a provider error.

## Acceptance evidence

Authoritative successful acceptance:

- Log: `/home/devitolo/paper-mini-rehearsal/production-acceptance-20260924-231143.log`
- Final root: `/home/devitolo/paper-mini-rehearsal/production-cutover/final-20260924-230636`
- UI check: Project Paper found; response size 334181 bytes
- Qwen production smoke: passed
- Gemini production smoke: passed
- Phoenix telemetry smoke: passed

The first cutover attempt failed and native service was restored. The second
cutover completed, but its main V2 log contains noisy early startup failures and
false-pass behavior while services were still becoming ready. Do not use that
log alone as success evidence. The separate production-acceptance log above is
the authoritative final check.

## Rollback retention and cleanup

Native directories, rehearsal folders, old containers and volumes, and rollback
evidence are intentionally retained through the weekend scheduled-job soak.
They are rollback assets, not duplicate active runtimes. Do not delete, prune,
rename, reuse, or partially merge them during validation.

Cleanup is deferred until the scheduled OpenAlex, arXiv, Semantic Scholar,
Gemini comparison, and backup paths have been observed as applicable and the
operator explicitly closes the rollback window. Before any cleanup:

1. Preserve the authoritative acceptance log and final-root evidence.
2. Confirm the live crontab still invokes only the container launcher.
3. Confirm both production containers are healthy/running and native services
   remain inactive.
4. Confirm expected scheduled logs, database writes, backup output, Gemini, and
   Phoenix behavior for the soak period.
5. Record exactly which old containers, volumes, directories, and native service
   files are approved for removal.

This document does not itself authorize cleanup or rollback. A rollback must use
the preserved cutover evidence and a separately approved operator procedure; do
not improvise by enabling native services alongside the containers.
## Curator model and scheduled-job safety

Production uses a standalone, internal MiniLM service for active Curator
interest fit, `qwen3:4b` for active Curator evidence assessment, and
`qwen2.5:1.5b-instruct` for review-field extraction. The app has no embedded
MiniLM inference dependencies or model mount. Deployment runs bounded MiniLM
and Qwen3 checks as post-deploy verification and fails the rollout if either
required check fails. Curator unloads Qwen3 after each candidate batch. A runtime
Qwen3 assessment failure falls back to the deterministic Curator score; a MiniLM
HTTP failure records unavailable interest fit without a score and does not
silently select the embedded backend or stop the source pipeline.

The 2026-10-20 main release established exact functional parity between the
retained embedded scorer and the standalone service for its selected Curator and
legacy raw-input cases. It retained the prior app/service images, production
configuration, and backup as rollback assets. Rollback changes images and
configuration over current authoritative state; do not automatically restore an
old database snapshot. A separate non-production app is running from source
revision `246483b33d7e4cbc285b592f6239097004e7ce7b`, built by Actions run
`39509451567`, but it is intentionally untested and its automatic health check
is disabled. No browser, HTTP, model, provider, pipeline, or resource checks
have run. The requested `127.0.0.1:18080` mapping is not effectively published
(only internal `172.23.0.2:8000` was observed), and its retained non-production
MiniLM service remains stopped. Do not describe this as validated or attach
production to non-production inference services.

Only one managed scheduled job runs at a time. An overlapping invocation is skipped for that day, exits successfully, and records the reason in `data/job-events.jsonl`. The Health page shows the event under **Scheduled jobs** and raises a warning when the most recent event for that job is `skipped_busy`.
