# Developer handoff: Mini non-production synthetic bootstrap

SDLC owns the one bounded setup window and consolidated user handoff; SRE verifies
production stability. Mini SSH is not authorized yet. All preparation here is
local. This is Mini nonprod, not the AWS draft, and does not enable production
promotion or modify production cron, repositories, containers or data.

## Reuse and change boundary

The existing `docker-compose.mini-nonprod.yml` is an opt-in exchange/model-sharing
contract. It requires an AWS destination secret and PDF source and defaults to
shared Ollama/MiniLM. Preserve it for later explicitly reviewed integration; do
not use it unchanged for initial synthetic isolation. The new standalone
`deploy/mini-nonprod/compose.bootstrap.yml` reuses the application's existing
serving startup, lifecycle lock and NON-PROD UI support with safer bootstrap defaults.
Never layer either file onto production or combine both nonprod files.

`paper_agents.web.render_app_header` already emits an escaped visible NON-PROD
badge and large NON-PRODUCTION watermark. Tests exercise this actual function.
No application source edit or production behavior change is needed. Serving mode
intentionally disables Scout/model execution in the bootstrap; full research
pipeline experiments need a later explicit model/scheduler policy.

## Fixed setup inputs

- Project `paper-mini-nonprod`; one app service; loopback `127.0.0.1:18080`.
- Five **external** volumes: `paper-mini-nonprod-data`, `paper-mini-nonprod-config`,
  `paper-mini-nonprod-logs`, `paper-mini-nonprod-runtime-control`,
  `paper-mini-nonprod-generated`. No volume is automatically initialized by app
  startup, and existing same-name state must not be overwritten.
- Internal Docker network `paper-mini-nonprod-internal`. No host gateway, secret,
  provider key, shared model/PDF mount, Docker socket or worker/scheduler service.
- Env input: only `PAPER_MINI_NONPROD_IMAGE`, exact reviewed AMD64 registry digest
  for an application image containing this runtime contract. Empty template fails.
  Verify actual Mini architecture and image/source evidence before selecting it.
  Do not reuse the AWS historical local image ID as a registry digest.
- Provisional test budget: 512 MiB memory, memory+swap also512 MiB (no swap),1 CPU,
  128 PIDs,16 MiB tmpfs, two1 MiB Docker log files. No automatic restart or pull.
  Read-only root; UID/GID10001; all capabilities dropped; no-new-privileges.
- `PAPER_AGENT_STARTUP_MODE=serving`, `PAPER_AGENT_LIFECYCLE_DIR=/runtime-control`.
  All five mounted directories must be owned by10001:10001 and usable. Lifecycle
  control is required by actual startup, not just an optional logging directory.
- Scheduler, Gemini, telemetry and MiniLM explicitly disabled. Sync identity is
  `mini-nonprod` / `mini-nonprod-disabled`; empty HMAC secret rejects sync requests.
  Ollama container-local port9 is deliberately unavailable. Internal network and
  absent credentials remain required; this URL alone is not an egress guarantee.

These names/port are planned values, not verified free resources on Mini. SDLC
must compare all actual production and nonprod identities before creating anything.
Do not lower resource reservations just to pass. SRE must verify sufficient RAM,
CPU/disk/inode capacity, no conflicting scheduled production work, and unchanged
production health. Suggested initial floor:1 GiB MemAvailable before startup;
retain512 MiB host headroom during bounded smoke testing. Account for dockerd and
image unpack overhead outside the app limit. Require at least2 GiB free disk after
measured image/fixture costs and existing backup reserve, with inodes below80%.
These are initial stop thresholds, not measured proof that the Mini can host it.
No resize, new cloud resources, package installation or automatic pruning.

## Local preparation already supported

From the reviewed checkout:

```bash
python3 -m unittest discover -s tests/mini_nonprod -v
python3 scripts/mini_nonprod_fixture.py /private/tmp/paper-mini-nonprod-fixture
```

The fixture command requires a new destination and rejects symlinked parent paths.
It creates a new SQLite schema with required sync metadata, one synthetic paper
and feedback row, empty profile/topics/industry inputs, a synthetic marker, empty
logs/control/generated directories, and a SHA256 file manifest. It validates
SQLite/foreign keys and actual `serving_state.validate` without migration. No
production data or secrets are read. Tests run actual `initialize`, lifecycle
lease, feedback persistence across another initialize, and overwrite refusal.

The selected container image must independently run `check-state` over the same
fixture before app startup. Repository tests do not prove image compatibility.
The fixture is seeded offline into NEW nonprod volumes only, with explicit
UID/GID10001 ownership. Empty production replacement volumes are never permitted.
No real production DB/config/PDF/profile may be copied under this setup scope.

## SDLC execution after one explicit SSH-window approval

Prepare actual host-local paths and reviewed image digest before requesting the
window. The approval must cover identity/resource inspection, transfer of only
reviewed bootstrap files and synthetic fixtures, creation of only absent named
nonprod objects, bounded startup/verification and stop. It does not cover any
production mutation, cron change, provider call or persistent remote-access grant.

1. Inspect actual Mini identity/architecture, current production image/mounts/health,
   schedules, Docker/Compose/cgroup capability and available capacity. Confirm18080
   and all planned nonprod names are free. Stop on ambiguity or existing data.
2. Verify exact reviewed image already local (pull-policy never). If absent, stop
   for a separately bounded artifact transfer/pull decision and disk check; never
   build on Mini or pull an unqualified tag. No production image pruning.
3. Create only the five absent nonprod volumes with
   `org.projectpaper.environment=mini-nonproduction` labels. Transfer/copy synthetic
   fixture files into corresponding volumes using a reviewed offline helper with
   no production mounts/network and bounded resources. Keep fixture manifest and
   inspect exact mount sources, owner and hashes before startup. Never use `cp`
   from production directories or chown a shared/production root.
4. Render explicit Compose with the nonprod env and file. Reject extra services,
   unexpected mounts/ports, or production names. Run image `check-state` offline
   against those volumes. The rootfs contains required schema/app code; absent
   lifecycle directory or mismatched schema is a failure, not a reason to reseed.
5. With only this file/env and explicit project, run one nonprod `up --no-build
   --pull never -d app`. Verify effective image/limits/mounts, readiness/runtime,
   UI watermark, `/`, `/topics`, `/health`, and a synthetic feedback write. Restart
   only nonprod and verify feedback persisted. Check no OOM/restart loop and
   production health/latency before/during/after. Prove internal-network egress
   restriction and reject sync authentication without invoking external providers.
6. UI access initially stays loopback. An approved SSH local-forward from user's
   Mac to Mini127.0.0.1:18080 can expose it without changing production routes.
   No public bind or Tailscale/production routing change is part of this package.
7. End initial window stopped unless SRE/user explicitly accepts continued app
   operation. Schedules remain disabled regardless; do not install a cron entry.

Use explicit `--project-name paper-mini-nonprod --env-file <reviewed-file> -f
<reviewed-compose>` for every Compose invocation; SDLC supplies verified absolute
paths, not guessed production paths. Stop uses `stop app`. Container/network
removal uses this exact project `down` **without** `--volumes`, `--rmi` or
`--remove-orphans`. Preserve volumes/fixtures for repeatability. Deleting data
requires separate approval and label/mount inventory; no global prune.

## Later shared-service policy — disabled now

PDF source reuse is implemented by `scout.shared_pdf_path`: resolved paths must
stay under the configured pool; existing nonempty files are reused and new
fetches stay in local data. Any future PDF-only bind must use read_only:true and
create_host_path:false, point at the verified PDF subtree only, and demonstrate
failed write/delete attempts. Never mount production DB/config roots. No PDF mount
is included now, so initial bootstrap cannot modify shared PDFs.

MiniLM is in-process CPU work over model artifacts, not a standalone inference
service. A future cache mount must be read-only, checksum-qualified, offline and
thread-bounded; no downloads or cache updates. Shared Qwen is inference-only via
an explicitly approved Ollama endpoint: no pull/delete/create/admin commands,
no production inference overlap, bounded tokens/time/concurrency, and an enforced
cross-environment admission policy. The existing nonprod-only lock is not proof
of coordination with production jobs. Service sharing must be reviewed against
actual Mini implementation before adding a network exception or mount.

Independent nonprod schedules remain disabled until schedule storage/identity,
production-wide resource exclusion, duration limits, kill/rollback behavior and
separate credentials are qualified. AWS production sync and paid provider calls
stay disabled. A future nonprod-only credential must never be copied from prod.
