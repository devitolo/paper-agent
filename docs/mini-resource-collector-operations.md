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

## Release identity and target routing

Standalone observability releases use exactly one target selector:
`observability-mini` for Mini or `observability-aws` for AWS. Versioned,
suffixed, or lookalike tags are rejected. The immutable release identity is
the triggering event SHA together with the archive checksum and AMD64 image
digest; every checkout is pinned to that SHA. A push of a valid target tag
automatically runs the serialized deployment for its matching target
environment (`observability-mini-production` or `observability-aws-production`).
The deployment still requires resource checks, checksum/digest verification,
health evidence, and rollback handling; no manual approval action is part of
the valid-tag path.


## Container package transaction recovery

The container installer/remover share `deploy/observability/recovery-common.sh`; retain it with the same checksum-verified release package. Root-owned recovery metadata and immutable backup copies live in `/var/lib/project-paper-observability/mini-production-control`. Do not delete, edit or move those files to collector-writable state.

On an installation error the installer attempts restoration automatically. If that restoration also fails, `PENDING-BACKUP` retains the prepared transaction. Resolve the reported filesystem/permission problem, then run the remover from the same verified package:

```sh
sudo bash deploy/observability/remove_container_cron.sh
```

Pending recovery restores the previously active installation and its previous recovery pointer. A root-owned `RECOVERED-PENDING` record makes repeats preserve that installation; the next successful installation clears that record. Ordinary rollback of a successfully installed version restores its predecessor and clears the active pointer, so repeating it is a no-op.

Missing, malformed, incomplete, altered or symlinked recovery inputs cause a nonzero result before restoration starts. Preserve the evidence and reconcile the error; do not force success by deleting the active pointer. File restore and completion cleanup can be retried because backup copies and the original manifest are not consumed.

These paths are tested against injected command/file failures in disposable Linux. They do not establish power-loss/fsync guarantees or replace separate production smoke and actual Docker-daemon cleanup checks.
