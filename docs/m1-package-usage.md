# Public Compose package

This installs the public app image and local model runtime, with manual discovery from the Review Queue. Ubuntu x86-64 is the first supported packaged path. The same v0.1.3 image passed macOS Apple Silicon acceptance on 2026-09-12. The existing native Mini deployment is unchanged.

The supported pairing is Linux x86-64 with Docker Engine/Linux amd64 containers. macOS Apple Silicon with Docker Desktop/Linux arm64 containers also passed acceptance. CPU inference is the baseline. Have Git and a writable clone of this repository, and start Docker with Compose v2 first; no host Python, Ollama, cron, systemd, or provider keys are needed. The installer checks provisional Docker RAM (4 GiB) and installation-filesystem free space (6 GiB) thresholds. These are configurable preflight guards; Docker's separate VM/disk-image capacity must also be sufficient.

From a clean clone, run:

```bash
cd /absolute/path/to/paper-agent
bash scripts/install_project_paper.sh
```

The defaults select Project Paper `v0.1.3` by immutable index digest `sha256:116e994bef7294767df08754c20870c596946a916e4e66f047efc973c858df13` and the pinned multi-platform Ollama image `docker.io/ollama/ollama@sha256:684d8674b4315fa18f4f0e973a118ec2652ed96f67563277839985175858e0ba`. Docker selects the matching container platform automatically. The installer rejects `latest` for release installs, validates Project Paper OCI source/version labels, and records the exact selected image references. Developers can still use `--build --ollama-image ollama/ollama:latest` for an explicit local build; that path is outside release qualification.

`prepare-model` is included in the Project Paper app image and talks only to the private Ollama HTTP API. It never mounts or executes a checkout helper and does not require the Ollama CLI in the app image.

The installer creates `.env` from `.env.example` only when absent. Existing `.env` values are preserved; set literal unquoted values for the documented package keys. `PAPER_PORT` defaults to 8000 and may be changed if occupied. Gemini is disabled in this package. arXiv is the default topic source; initial topics and profile interests are empty. The package does not import the creator's papers, configuration or learned profile. Optional-provider setup is later work.

The installer records its directory, platform and selected images in `.paper-install`. Keep this file: its project identity selects the persistent named volumes. The first setup may take time while Ollama downloads Qwen. Preparation is limited to 1800 seconds and two pull attempts by default; the final inference check is limited to 120 seconds. These values await hardware qualification. App startup does not wait for Ollama, so saved papers remain available during model failure.

The first installation record is saved only after both images are acquired and their platforms validated, before any services or volumes are created. If image acquisition fails before that point, correct the reference in `.env` and rerun; it is not treated as an upgrade. Once the record exists, normal reruns retain those image selections.

On success, open the printed loopback URL (normally `http://127.0.0.1:8000`). `/ready` reports app/database readiness; `/runtime` distinguishes API availability and model presence. Presence alone is explicitly **not** proof inference works: the installer and manual Scout worker each perform a bounded generation check. Model preparation logs report waiting, pulling, failure or model availability. Ollama is reachable only on the private Compose network; the app is published only on host loopback.

## Privacy and Credentials

The default package sends arXiv requests, pulls Docker/model assets, and performs local Qwen inference through the Compose Ollama service. It does not require or use OpenAI, Gemini, OpenAlex, or Semantic Scholar credentials. No accounts, public serving, or telemetry are part of M1.

Feedback blobs, parsed scores, recommendations, artifacts, profile state, and topics remain in local Compose volumes. Treat `paper-data` as private user data because it can contain paper notes, feedback, and profile preferences. `.env` is created with mode `600`; keep provider keys out of screenshots, logs, and bug reports. Optional Gemini/container authentication remains a later architecture task, so packaged feedback save does not spawn Gemini.

## First manual discovery

Open **Topics** and use the existing topic controls to add or enable a topic with **arXiv** selected. Return to **Review Queue** and select **Run Scout**. The action stays disabled until an enabled arXiv topic exists, the background bounded inference check succeeds, and no Scout job owns the lock. Topic editing and Topic Agent behavior have not been redesigned. Manual discovery uses every enabled arXiv query, including topics whose cadence is manual; OpenAlex and Semantic Scholar topics are ignored by this action.

For a first example, enter `Scout AIOps papers` in Topic Agent with arXiv selected, choose **Ask TopicAgent**, inspect the proposal, then **Apply and save**. Expand the configured topic list, choose **Edit**, set **Query** to `AIOps`, leave **Enabled** checked and arXiv selected, and **Save**. That exact query produced recommendations in the recorded macOS acceptance; current results can differ. Cadence controls do not install a schedule in this package.

The worker checks Qwen, then runs the existing Scout → Curator → Reviewer pipeline. It fetches up to 20 candidates in one Scout attempt, keeps at most three recommendations, uses a five-second request delay, two source retries, 60-second source timeout, and quick extraction with two chunks/one worker and a 120-second model-call timeout. The whole manual job has a 30-minute deadline. These are conservative execution limits, not ranking changes or a promise that every query yields papers.

The panel updates queued/running/completed/empty/failed/interrupted status while you continue reviewing. On completion, choose **Refresh papers** to load recommendations; the page does not automatically reload and discard an unfinished feedback draft. Empty runs suggest correcting topics or retrying later. Source/model failures keep saved papers available and point to Runtime, Health and `data/scout-last.log` inside the app data volume. Retry is explicit; no schedule or automatic rerun is installed.

Web requests and direct full `pipeline-daily` invocations share the data-volume `scout.lock`, so duplicate clicks or another container cannot launch overlapping full pipelines against that data directory. Latest status is persisted in `scout-status.json`. Restart reconciles an abandoned queued/running state to interrupted once no process holds its lock; it never relaunches the job. A still-running owner remains active. Do not delete lock/status files while work is running. The most recent manual-run log is replaced on the next explicit run.

## Review and feedback

Open a recommended paper by its title or **Open PDF**. **Match Score** is Curator ranking; **Your score** is your saved feedback rating. Use **Copy discussion prompt**, paste it into your own ChatGPT conversation, then paste the final feedback blob into **Add feedback** → **Save feedback** on the same paper. ChatGPT access is separate from installation; Project Paper does not send that handoff automatically. Local `/artifact/...` links in the copied prompt are not accessible to ChatGPT; use the public paper link or supply the paper yourself.

The prompt requests Decision, Score, Reason, signals, and what you want more/less of. A score such as `Score: 4.5` must be between 1 and 5. Reopen **View/edit feedback** to confirm the exact saved text, and check **Your score** when parsing succeeded. Saving another version preserves raw feedback history. Unparsed text is still retained. Gemini profile synthesis is disabled; feedback can inform deterministic Scout guidance, but saving does not promise a newly synthesized profile.

## Runtime ownership and recovery

Docker Compose owns the app and Ollama services, both with `unless-stopped` restart policies; `prepare-model` is a finite helper with no automatic restart. Docker must be running for the UI and discovery to work. The app runs as the non-root `paper` user (UID 10001). No host Ollama, cron or systemd service is installed.

To restart, repair an interrupted setup, or retry a failed model preparation, invoke the same installer by its full path from any directory:

```bash
bash /absolute/path/to/paper-agent/scripts/install_project_paper.sh
```

It keeps the same configuration, images and volumes. It does not rebuild a cached app image or upgrade to a new selected image. If the source checkout changes, an ordinary installer rerun still uses its cached selected image. Development rebuilds must be explicit; public upgrades require the later compatibility/backup workstream.

The installer prints diagnostic commands with the recorded project name. From the install directory, replace `RECORDED_PROJECT_NAME` with the `project=` value in `.paper-install`:

```bash
docker compose -p RECORDED_PROJECT_NAME logs --tail 100 app ollama prepare-model
docker compose -p RECORDED_PROJECT_NAME stop
docker compose -p RECORDED_PROJECT_NAME up -d --no-build --pull never --force-recreate app ollama
docker compose -p RECORDED_PROJECT_NAME exec -T app python -m paper_agents.package_runtime check-model
```

For discovery diagnostics, open **Health** (`/health`) for database integrity, workflow/source counts, artifact gaps and feedback status. `/ready` only checks database readability; it is not an integrity or model check. From the installation directory, these read-only commands inspect the packaged data:

```bash
docker compose -p RECORDED_PROJECT_NAME exec -T app python -m paper_agents.cli db health --days 21
docker compose -p RECORDED_PROJECT_NAME exec -T app tail -n 100 /app/data/scout-last.log
```

The Scout log exists after a manual run starts. Inspect it before retrying because the next run replaces it. An empty run may reflect deduplication or no eligible results; a completed run can still have extraction warnings in Health.

Data, artifacts and profile live in `paper-data`; mutable topics live in `paper-config`; model weights live in `ollama-data`, all scoped to the saved project name. Stop/start and container recreation retain these volumes. **Do not remove volumes or change the project name to fix an installation.** A failed startup preserves malformed state and reports an error rather than silently replacing it. Moving the installation directory or importing native data requires a separate explicit migration.

For the packaged runtime, normal backup/restore and upgrades are not yet public-release features. Before relying on the package with durable personal data, export or copy the `paper-data` and `paper-config` volumes with Docker-native tooling while containers are stopped, then verify the SQLite database with `PRAGMA integrity_check`. Retain `.paper-install`, a private copy of `.env`, the matching checkout/helper and image selections alongside the backup. Volume names are project-scoped; repository files alone do not back up user data. This is a preservation precaution, not a tested restore or rollback procedure. The native Mini `scripts/backup_db.sh` remains available for checkout-based operations, but it is not yet the packaged-volume backup contract.

If a process was forcibly killed and left `.paper-install.lock`, first verify no installer is running, then remove that empty lock directory and retry. A model-preparation service already running is reused rather than launching a second pull. Timeout and resource failures leave logs, app and volumes available for inspection. No automatic scouting is installed.

## Support and Known Limits

Support is through [GitHub Issues](https://github.com/devitolo/paper-agent/issues) for the current major version and one prior major version. Include the exact commit, host OS/architecture, Docker/Compose versions, installer command, `.paper-install` project name, and redacted logs. Do not include `data/paper_agent.db`, raw feedback, `.env`, or full paper artifacts unless intentionally sanitized.

## Linux release acceptance

Maintainers can qualify the exact public image on a fresh GitHub-hosted Ubuntu x86-64 runner. Open **Actions → Release Acceptance → Run workflow**, retain the immutable default app and Ollama digests, and start the run. The manual workflow runs the normal installer, Qwen preparation and inference, live arXiv discovery, topic and feedback form submissions, persistence across app recreation, and interrupted-run recovery. It uploads status and service logs for review and removes its temporary containers and volumes even after failure. A transient external source or registry failure is a failed run with evidence; rerun only after confirming the failure was external.

The same harness can qualify a fresh macOS Apple Silicon checkout. Set `PAPER_ACCEPTANCE_PORT` to an unused loopback port if another installation uses 8000. The harness refuses a checkout containing `.env` or `.paper-install`, and it removes only the unique temporary Compose project's volumes.

Known M1 limits:

- Ubuntu x86-64 passed the full exact-image acceptance workflow on 2026-09-11.
- v0.1.3 passed full automated fresh-install acceptance on Ubuntu x86-64 and macOS Apple Silicon; earlier source and model failures remain documented in the release acceptance report.
- The public Project Paper image has arm64 and amd64 variants with SBOM and provenance attestations.
- No packaged automatic schedule is installed.
- OpenAlex, Semantic Scholar, and Gemini are not part of the default packaged flow.
- Upgrade/rollback and full packaged backup/restore remain V1 work.
