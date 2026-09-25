# Mini Compose and scheduler: unqualified local slice

> Historical slice record. Current first-cutover recipe: [mini-migration-candidate.md](mini-migration-candidate.md). Gemini startup behavior and the parked scheduler status below are superseded by that recipe.

This is an implementation contract with synthetic tests, not production migration
clearance. No image was built/pulled, no service or native schedule was changed,
and no model, source or provider request was made during development.

## Explicit deployment contract

`docker-compose.mini-migration.yml` is standalone: it must be explicitly selected
with `-f`, never implicitly merged with fresh `docker-compose.yml`. Its project
identity is mandatory and fixed across future deployments. Fresh Compose is
unchanged. App/scheduler/finite model verifier use the same selected migration
image; Ollama has a distinct private service/image/cache and no published port.
Image references must contain full digests; runtime checks reject tags. No image
pull is implicit (`pull_policy: never`), and this file has no build directive.
All restart policies are currently `no`: runtime/restart qualification is pending.

Required unresolved image, project, port, manifest, credential, DNS and volume
settings use Compose's nonempty-required interpolation. The example environment
is deliberately incomplete. The scheduler additionally has an opt-in Compose
profile and `PAPER_AGENT_SCHEDULER_ENABLED=0` default. Blank choices are not filled
from fresh-install configuration. Do not paste the example as a ready deployment.

App and scheduler share exactly the same data/config mounts and DB inode, logs,
backup destination, runtime control mount, read-only approved manifest and
integration settings. These are external, preprovisioned isolated volumes: their
absence fails rather than silently creating fresh empty authoritative state.
UID10001 must own the destination control/log/backup volumes and have required
state access. Provisioning/ownership changes and native import transfer remain
outside this slice. Logs mount at `/app/logs`, weekly SQLite backups at `/backups`.

Selecting this standalone file explicitly opts into the existing external network
`paper-observability_default`; app/scheduler use a REQUIRED independently verified
stable `PAPER_PHOENIX_DNS` name. No DNS name is guessed. Phoenix storage/container
is not recreated. Only app UI is published, on loopback and an explicitly selected
port. No host networking, transient IP, Docker socket or whole native home mount.

The app has no direct or transitive dependency on Ollama or model verification:
it can browse preserved content when inference is unavailable. The finite helper
is separately opt-in via the `model-verification` profile and only verifies the
inventoried Qwen digest. It can fail if Ollama
is not yet ready and must be explicitly rerun; it never retries by downloading.
Compose completion/health dependencies are ordering aids, not the writer barrier.
CPU/memory limits remain unqualified and are not guessed from total host RAM.

## Initialization and writer lifecycle barrier

A dedicated `.runtime.lock` lives in `/runtime-control`, OUTSIDE authoritative
`data/` and `config/`. The cleared imported-state manifest/receipt logic is unchanged.
All participants use the same mounted lock inode. Opens use no-follow/nonblocking
flags, reject special/multiply linked files, and validate descriptor/path identity.

Imported app startup takes an exclusive lease before initialization. If another
app or an active job holds a shared lease, startup fails busy without initializing.
After successful imported initialization, the app converts to a shared lifetime
lease before serving. A platform may release/reacquire during flock conversion;
the app does not serve yet, and an intervening initializer still takes exclusive
ownership. Existing web DB initialization finishes before its HTTP server serves.
Recreation therefore requires old app shutdown and either completion or bounded
stopping of current work. Do not spin-retry a failed initializer around active jobs.

Idle scheduling does NOT hold the runtime lease. Each dispatch acquires shared
nonblocking, verifies the SAME manifest-bound import receipt, probes app `/ready`
with a two-second total POSIX wall deadline and an8KiB response cap, and retains its lease through child cleanup.
Not-ready/initializing/busy outcomes release the lease and record a skip. No
scheduler initialization call exists. If readiness handling consumes the eligible
minute, dispatch records `minute_expired` instead of launching late.

Manual child startup inherits an already acquired shared descriptor before the
parent releases it. It verifies descriptor/path identity before status/DB work and
closes it at completion, so parent death between spawn and child startup cannot
open an initialization race. Only explicitly intended descriptors cross exec.
The app's own shared lease covers web and in-process feedback/readiness writers.

Supported writers are this app/manual child and scheduler-owned jobs. Ad-hoc native
or container CLI writers do not automatically participate in the lifecycle protocol;
keep them disabled during cutover/recreation. Native cron and native web must be
inactive before enabling the replacement. The future qualified Gemini child/auth
path needs its own final lifecycle review; this slice does not clear it.

## Fixed schedule and durable outcomes

The clock is timezone-aware America/Los_Angeles:

| Local time | Job |
| --- | --- |
| 04:00 daily | existing OpenAlex wrapper, slot0 |
| 05:00 daily | existing arXiv wrapper, slot0 |
| 06:00 daily | existing Semantic Scholar wrapper, slot0 |
| 02:00 Monday | existing biweekly profile comparison, anchor2026-09-07 |
| 01:00 Sunday | existing SQLite backup script, explicit DB and `/backups` arguments |

Afternoon slots are absent. Existing pipeline CLI budgets, source behavior and
ranking are not reimplemented. Environment pins the existing cadence/slot and
turns self-update off. The biweekly wrapper is still invoked on eligible anchor
dates; other Mondays are explicitly recorded `outside_biweekly_cadence` skips.
Profile comparison remains a DB writer despite its dry-run profile semantics.

Only the current local scheduled minute is eligible. No missed-minute backfill,
historical catch-up, automatic retry or replay of uncertain work. Claims use
job + local date + scheduled HHMM, so a fall-back repeated hour cannot run a slot
twice; a nonexistent spring minute is skipped. A scheduler stopped during a minute
may claim it on restart only if the minute is still current and no prior claim exists.

Before launch, a synchronous SQLite transaction commits the unique claim to the
persistent scheduler journal in `/app/logs/scheduler.sqlite`. A crash after claim
but before spawn consumes the slot. On restart, claimed/running records become
`interrupted`; they are never replayed. A corrupt/unknown journal fails closed. Reopening validates the exact supported
table definition, column metadata, primary-key index and index columns, and absence
of extra schema objects BEFORE status updates or claims. Unknown constraints are
rejected without repair or row changes.
Durability depends on filesystem SQLite/fsync support; disk failure is not reported
as successful scheduling. Final outcomes include completed, skipped, failed and
interrupted with fixed reason codes and exit codes, not provider text or arguments.

A separate singleton lock is held by scheduler and intentionally inherited by the
owned watchdog/job. A replacement scheduler cannot start another job while an
orphaned previous watchdog/job is still alive. The runtime lease is inherited too.
Each scheduler owns at most one child at a time and continues observing due minutes;
other due jobs are durably skipped as scheduler_busy. The packaged pipeline's
existing DB-parent `scout.lock` arbitrates manual/source runs. Scheduler preflight
releases that lock immediately; it never nests the pipeline lock across dispatch.
A race lost by the CLI returns reserved exit75, recorded as pipeline_busy skip.
Shell wrappers use the same opt-in busy code, retaining native default exit0.

This serializes source/manual work compared with native independent source locks.
Busy schedules are skipped, not queued or retried. This operational change needs
explicit approval in the eventual cutover package.

## Process supervision and privacy

The default per-job wall budget is1800s (validated maximum7200s); termination grace
is5s (range3..30). These are provisional, not measured Mini budgets. The scheduler
and an independent watchdog both enforce bounds. The watchdog retains the leases,
starts the original wrapper in its own process group, and kills/reaps that group
after completion, timeout or termination, including resistant descendants.
A scheduler SIGKILL does not disable the surviving watchdog's own deadline.
Scheduler TERM/INT cleanup is bounded; repeated force-kills of the watchdog itself
or host failure still require operational recovery/lease observation, not blind replay.

Raw child stdout/stderr go to `/dev/null`; no provider responses, key values, paper
text or raw exceptions are persisted by the scheduler. Persistent logs are the
structured job journal. This sacrifices detailed provider debug output intentionally;
separate narrowly scoped diagnostics may be needed for a failed job. Docker log
rotation is bounded. No provider is called to test scheduling.

## Blocking gates and backup procedure

Live scheduler entrypoint remains explicitly blocked by the unqualified Gemini
runtime/auth gate. Gemini=0 cannot silently remove Monday work: enabled scheduling
rejects incomplete parity. Gemini=1 still hits the first-slice app startup block.
The tested scheduler loop is wired behind that gate for the later qualification
slice; no environment bypass flag is supplied here.

Before ANY offline full-state backup/restore or recreation, stop/quiesce scheduler,
its watchdog/job descendants, manual worker, app and any separately authorized
writer; verify runtime leases released. Stopping only app is insufficient. Existing
package backup's shared-volume running-container rejection must remain in place.
The weekly SQLite online-backup job is not a full-state snapshot.

Do NOT use current `scripts/package_backup.sh` as a migration-stack runbook: it
still selects the fresh Compose file. Later work must explicitly select this stack
identity/file and verify all writer stops before copying state. Do not remove its
volume-use guard to get around a scheduler. Runtime control files and job journal
are operational state; DB/profile/config/artifacts remain authoritative research
state. Restore/reversal after new feedback still requires a separate rehearsal.

Pending actual image build and AVX-only/runtime qualification,
resource limits, Gemini executable/auth, real Phoenix DNS/export, provisioned
volumes/state import, full-state restore/reverse transfer, image publishing,
restart policy, and production cutover approval. No synthetic pass clears them.


## Bounded independent clearance — 2026-09-21

Architecture reported independent QA CLEAR for this scheduler/Compose slice:
21 scheduler tests passed. Independent recheck verified that the original
missing-primary-key journal fixture is rejected with database bytes and directory
contents unchanged; a valid journal suppresses a repeated claim after reopening;
the app dependency graph has no model-readiness dependency; and diff checking is
clean. Architecture accepted this bounded local slice only.

For comparison, the developer's final focused regression run covered 58 tests
(21 scheduler, 22 foundation, 15 manual) and passed in 5.831 seconds. That broader
run is developer evidence, not an assertion that independent QA reran all 58.
The code remains uncommitted in `codex/mini-migration-foundations` based on
`b4b3fc6`. No commit, image build/publish, live runtime validation or cutover is
covered by this clearance.

Gemini live enablement remains blocked. SDLC is researching version-specific
API-key provisioning for the inventoried CLI; the actual key location and
effective model settings remain unresolved. No secret copying has been requested.
Further runtime/auth implementation awaits a separately settled contract. All
other runtime, recovery, deployment and cutover gates listed above remain open.
