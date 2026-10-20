# Mini UI and pipeline roles

The shared `mini-production-http` image supports explicit
`PAPER_AGENT_RUNTIME_ROLE=ui` or `pipeline`. The default remains `ui` for existing
installations. Pipeline startup does not import or start the web server. It keeps
the existing initialization and lifetime shared runtime lease, then waits for
SIGTERM/SIGINT. Host cron still enters the running `app` service through
`mini_container_job.sh` and `migration_job`; the process does not launch schedules.
All discovery, profile maintenance, backup, model API and tracing code remains in
the same image. Removing UI means removing its process/listener/port, not deleting
shared application code or altering scoring.

`python -m paper_agents.package_runtime check-pipeline` checks the pipeline
process identity and SQLite readability locally. It makes no HTTP, model, or
provider request. The readiness marker is container-local under `/tmp`, includes
the Linux process start identity, and is removed on orderly shutdown. Production
retains normal imported-state initialization, Gemini local configuration checks,
and HTTP MiniLM readiness at startup; none of these generates model output.

## Deployment contract

- Production retains service `app`, existing state/config/control/log/backup
  volumes, secrets, provider settings and network attachments. Set its role to
  `pipeline`, remove all published ports and replace the HTTP `check-app`
  healthcheck with `check-pipeline`. The reference override is
  `deploy/mini-roles/production-pipeline.override.yml`. Its settings must be merged
  into the **existing production override named by live cron**, not just appended
  to a one-time command. `ports: !reset []` needs a supporting Compose version;
  inspect the rendered result to confirm there are no ports.
- Nonprod UI retains `paper-mini-nonprod-app-1` and its five
  `paper-mini-nonprod-{data,config,logs,runtime-control,generated}` volumes. Append
  `deploy/mini-roles/nonprod-ui.override.yml` to the retained contract. Its
  NON-PRODUCTION watermark and disabled provider/sync/schedule settings remain.
- New nonprod pipeline uses `deploy/mini-roles/nonprod-pipeline.yml`, project
  `paper-mini-nonprod-pipeline`, service `pipeline`, and five distinct external
  `paper-mini-nonprod-pipeline-{data,config,logs,runtime-control,generated}` volumes.
  Provision only these new volumes with new synthetic input from
  `scripts/mini_nonprod_fixture.py`; never seed or reset existing UI/production
  volumes. Its SQLite database is `/app/data/paper_agent.db` in its own data
  volume. It has no port, no healthcheck and no active schedules or providers.
  It uses only the existing private nonprod network; no production inference
  network attachment or service change is required.

Both nonprod containers are intentionally unsynchronized. The testing hold
disables automatic healthchecks; creation/running metadata is not functional
acceptance. No nonprod smoke, browser, inference or pipeline exercise accompanies
this change. The existing internal-network host-port publication issue remains:
requesting `127.0.0.1:18080` does not establish usable browser ingress. Do not
broaden network egress/public ingress to hide this limitation.

## Release and rollback

Manually dispatch `.github/workflows/mini-runtime-roles.yml` **from main**. It builds
one immutable AMD64 image through hosted Actions, checks production worker
lifecycle and cron compatibility, and records source/digest evidence. It does not
deploy, rebuild the standalone MiniLM service, or test nonprod. SDLC is the sole
Mini operator: reconcile current state, quiesce actual jobs, take a consistent
backup, retain the previous image/env/override/port configuration, then replace
the app. Confirm local pipeline readiness, no UI listener/port, cron exec command
compatibility, and unchanged authoritative state before releasing all fences.
If this fails, restore the prior image and runtime configuration with the current
data; never restore a stale database merely to reverse an image change.

Do not use `mini_production_update.sh` for this release: that legacy path expects
HTTP readiness and rewrites cron. The parked opt-in scheduler is not activated;
host cron remains authoritative. AWS is untouched. Data exchange/sync is deferred,
so AWS feedback/topic changes do not reach the Mini pipeline, and no ad hoc state
copy is performed to conceal this gap.
