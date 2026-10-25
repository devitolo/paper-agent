# Mini resource collector operations

This procedure covers the host collector for Mini production. It is
separate from paper discovery, SQLite sync, cron, and AWS delivery. The
collector does not write the Paper database and does not send metrics to AWS.

## What the package provides

The release contains the read-only collector, a locked one-minute cron wrapper,
a root-owned environment template, and bounded-retention
tests. The collector samples CPU counter deltas, memory and swap, filesystem
capacity/free/inodes, optional thermal sensors, cgroup memory, and an explicit
allowlist of production container state. Container observations may include
health, restart/OOM state, image identity, and revision labels. It stores raw
samples for seven days and complete UTC-aligned summaries for 90 days.

The package does not install host files, install a cron entry, grant Docker access,
change Compose, mount the status file into the UI, or configure Mini-to-AWS
transport. Those are deliberate host/operator steps.

## Manual host steps

SRE must perform these steps on the Mini production host using the approved
host-install path. Do not run them against AWS during this phase.

1. Install the standalone host package with
   `scripts/install_resource_collector_cron.sh --apply`. It installs the
   collector script, locked cron wrapper, and environment file as
   root-owned files. Keep the script immutable to the cron user.
2. Create `/var/lib/project-paper-observability/mini-production` with ownership and
   mode that permit only the collector to write and the UI consumer to read the
   sanitized status file.
3. Set `PROJECT_PAPER_RESOURCE_HOST`,
   `PROJECT_PAPER_RESOURCE_ENVIRONMENT=mini-production`, the state root, and the
   fixed production container-name allowlist. Leave the allowlist empty if
   Docker inspection is not approved; the result must be `unconfigured`, not a
   guessed healthy value.
4. Make `/var/lib/project-paper-observability/mini-production-export` available
   to the production UI through a read-only bind mount or an equivalent existing
   observability mount. Do not mount the
   Docker socket, `/proc`, `/sys`, environment files, secrets, or raw sample
   logs into the web container.
5. Add the dedicated one-minute cron entry from
   `/etc/cron.d/project-paper-resource-collector`. Verify the `flock` lock,
   cron cadence, state ownership, and the fixed production container allowlist.
   Cron must remain separate from Paper discovery jobs.
6. Observe at least one raw sample and one complete 15-minute summary. Verify
   restart persistence, bounded file growth, atomic replacement, correct
   `mini-production` identity, and stale/unknown behavior after an interrupted
   collection.

## Rollback

Run `scripts/remove_resource_collector_cron.sh`, retain the collected
   evidence for review, and remove only the collector unit/configuration and
   its dedicated observability directory. Do not remove Paper data, Compose
   volumes, cron entries, sync credentials, or application logs. Removing the
   read-only UI mount is a separate reversible step.

## Acceptance boundary

Metrics are accepted only when the status record has a valid schema,
`observed_at`, window bounds, sample coverage, scope, and collector identity.
Missing records are `unknown`; unreadable or malformed records are distinct
`unavailable`/`malformed` states; data older than three collection intervals is
stale. Host-wide readings remain labeled host scope. No Mini-to-AWS metrics
transport is implied by this procedure.
