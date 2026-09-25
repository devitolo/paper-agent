# Mini production container operations

This runbook records the production state established on 2026-09-24. It is for
the Project Paper Mini operator, not the public first-user package.

## Current runtime

| Component | Production state |
|---|---|
| App | `paper-mini-production-app-1`, image `sha256:817716e84db53235c0ec082323880f3af4c2e62843699749336b84ff89115eb9`, healthy, published as `127.0.0.1:8000->8000/tcp` |
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

Confirm the two production containers and loopback publication:

```sh
docker ps --filter name=paper-mini-production
docker inspect --format '{{.Name}} {{.Image}} {{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' \
  paper-mini-production-app-1 paper-mini-production-ollama-1
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
| Gemini profile comparison | `profile` | biweekly cadence checked by the Monday 02:00 wrapper; dry-run only |
| SQLite backup | `backup` | Sunday 01:00 |

The launcher executes the corresponding wrapper inside the app container and
holds the shared runtime lifecycle lease across preflight, writes, descendant
cleanup, and completion. Cron remains the only scheduling authority; the parked
container scheduler must remain disabled.

The committed `deploy/project-paper.crontab` uses the container launcher. The
installer refuses templates that still contain native `.venv` pipeline commands.
Inspect the live schedule with `crontab -l` before and after any change.

## Releases

Use [Mini release runbook](mini-release-runbook.md) for application updates. The
release path is image-based: build and accept a `mini-production` image from a
committed revision, then pull that immutable digest on the Mini and recreate the
existing `app` service. Do not deploy application code by `git pull`, and do not
rebuild on production.

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
