# M1 Compose package (development, not a published release)

This installs the app and local model runtime, with manual discovery from the Review Queue. Fresh-machine/source/model acceptance remains M1.4; unit tests alone do not establish it. The existing native Mini deployment is unchanged.

The initial target pairings are macOS Apple Silicon with Docker Desktop/Linux arm64 containers, and Linux x86-64 with Docker Engine/Linux amd64 containers. Neither pairing is claimed qualified until QA records actual install/model/source results. CPU inference is the baseline. Install Docker with Compose first; no host Python, Ollama, cron or provider keys are needed. The installer checks provisional Docker RAM (4 GiB) and installation-filesystem free space (6 GiB) thresholds. These are configurable preflight guards, not measured minimum requirements; Docker's separate VM/disk-image capacity must also be sufficient.

From a clean clone, choose an Ollama image reference explicitly, then run:

```bash
bash scripts/install_project_paper.sh --build --ollama-image YOUR_SELECTED_OLLAMA_IMAGE_REFERENCE
```

Replace the uppercase argument with an actual image reference; it is not a pullable image name. Release engineering must assign and qualify the Ollama digest and the single multi-architecture Project Paper image before publication. `--build` creates `project-paper:local-m1` for the current architecture; it does not publish or demonstrate a multi-platform manifest. A published package will select its pinned image using `--app-image` without `--build`.

The `prepare-model` script currently uses a bind mount from this checkout. This is development packaging only: publication must package that helper as a versioned, pinned release artifact rather than depend on mutable checkout content.

The installer creates `.env` from `.env.example` only when absent. Existing `.env` values are preserved; set literal unquoted values for the documented package keys. `PAPER_PORT` defaults to 8000 and may be changed if occupied. Gemini is disabled in this package. arXiv is the default topic source; initial topics and profile interests are empty. The package does not import the creator's papers, configuration or learned profile. Optional-provider setup is later work.

The installer records its directory, platform and selected images in `.paper-install`. Keep this file: its project identity selects the persistent named volumes. The first setup may take time while Ollama downloads Qwen. Preparation is limited to 1800 seconds and two pull attempts by default; the final inference check is limited to 120 seconds. These values await hardware qualification. App startup does not wait for Ollama, so saved papers remain available during model failure.

The first installation record is saved only after both images are acquired and their platforms validated, before any services or volumes are created. If image acquisition fails before that point, correct the reference in `.env` and rerun; it is not treated as an upgrade. Once the record exists, normal reruns retain those image selections.

On success, open the printed loopback URL (normally `http://127.0.0.1:8000`). `/ready` reports app/database readiness; `/runtime` distinguishes API availability and model presence. Presence alone is explicitly **not** proof inference works: the installer and manual Scout worker each perform a bounded generation check. Model preparation logs report waiting, pulling, failure or model availability.

## First manual discovery

Open **Topics** and use the existing topic controls to add or enable a topic with **arXiv** selected. Return to **Review Queue** and select **Run Scout**. The action stays disabled until an enabled arXiv topic exists. Topic editing and Topic Agent behavior have not been redesigned. Manual discovery uses every enabled arXiv query, including topics whose cadence is manual; OpenAlex and Semantic Scholar topics are ignored by this action.

The worker checks Qwen, then runs the existing Scout → Curator → Reviewer pipeline. It fetches up to 20 candidates in one Scout attempt, keeps at most three recommendations, uses a five-second request delay, two source retries, 60-second source timeout, and quick extraction with two chunks/one worker and a 120-second model-call timeout. The whole manual job has a 30-minute deadline. These are conservative execution limits, not ranking changes or a promise that every query yields papers.

The panel updates queued/running/completed/empty/failed/interrupted status while you continue reviewing. On completion, choose **Refresh papers** to load recommendations; the page does not automatically reload and discard an unfinished feedback draft. Empty runs suggest correcting topics or retrying later. Source/model failures keep saved papers available and point to Runtime, Health and `data/scout-last.log` inside the app data volume. Retry is explicit; no schedule or automatic rerun is installed.

Web requests and direct full `pipeline-daily` invocations share the data-volume `scout.lock`, so duplicate clicks or another container cannot launch overlapping full pipelines against that data directory. Latest status is persisted in `scout-status.json`. Restart reconciles an abandoned queued/running state to interrupted once no process holds its lock; it never relaunches the job. A still-running owner remains active. Do not delete lock/status files while work is running. The most recent manual-run log is replaced on the next explicit run.

To restart, repair an interrupted setup, or retry a failed model preparation, invoke the same installer by its full path from any directory:

```bash
bash /absolute/path/to/paper-agent/scripts/install_project_paper.sh
```

It keeps the same configuration, images and volumes. It does not rebuild a cached app image or upgrade to a new selected image. If the source checkout changes, an ordinary installer rerun still uses its cached selected image. Development rebuilds must be explicit; public upgrades require the later compatibility/backup workstream.

The installer prints diagnostic commands with the recorded project name. From the install directory, replace `RECORDED_PROJECT_NAME` with the `project=` value in `.paper-install`:

```bash
docker compose -p RECORDED_PROJECT_NAME logs --tail 100 app ollama prepare-model
docker compose -p RECORDED_PROJECT_NAME stop
docker compose -p RECORDED_PROJECT_NAME up -d --force-recreate app ollama
docker compose -p RECORDED_PROJECT_NAME exec -T app python -m paper_agents.package_runtime check-model
```

Data, artifacts and profile live in `paper-data`; mutable topics live in `paper-config`; model weights live in `ollama-data`, all scoped to the saved project name. Stop/start and container recreation retain these volumes. **Do not remove volumes or change the project name to fix an installation.** A failed startup preserves malformed state and reports an error rather than silently replacing it. Moving the installation directory or importing native data requires a separate explicit migration.

If a process was forcibly killed and left `.paper-install.lock`, first verify no installer is running, then remove that empty lock directory and retry. A model-preparation service already running is reused rather than launching a second pull. Timeout and resource failures leave logs, app and volumes available for inspection. No automatic scouting is installed.
