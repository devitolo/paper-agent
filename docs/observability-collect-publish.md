# One Mini cron: collect then publish

The existing `project-paper-observability-mini-production` cron retains its
`*/15` schedule and collector lock, but calls the fixed root-owned
`project-paper-observability-collect-publish` wrapper. It runs the existing
network-isolated collector first. Only successful collection starts the separate
non-root publisher container. There is no second schedule. Publisher failures
return nonzero and retain the exact body/ID for retries; they never rerun
collection within that invocation. The next scheduled cycle still collects.

The publisher uses the already-qualified image digest and existing dedicated
key/state. It requires Mini's verified Tailscale DNS resolver. The wrapper
retains the existing collector log redirection and rotation, bounded network
attempts, outer timeout and bounded container cleanup. Container logging is off;
only sanitized delivery state/UUID output enters the existing bounded cron log.
The common lock covers both stages, preventing overlap. No raw samples, business
volumes, host proc/sys or Docker socket are mounted in the publisher.

One-time root provisioning installs the wrapper and publisher.env and replaces
only the launcher token in the existing cron entry. The fixed deploy helper is
also updated to retain that wrapper on future Mini collector releases, after
checking its root ownership, type and non-writable permissions. AWS scheduling
and the collector launcher are unchanged. Qualification still calls the
collector directly; it does not publish during an image deployment.

`install_collect_publish.sh` verifies source hashes, existing path ownership,
private key/state prerequisites and pre-pulled image; acquires deployment and
collector locks; saves all managed prior files before enabling restoration; and
installs the combined command. An installation failure restores the originals.
It prints a root-only rollback script path. It does not run collection or
publication during installation. Cron will execute at the next quarter hour.

To stop publication while keeping collection, set
`PAPER_OBSERVABILITY_PUBLISH_ENABLED=0` in the root-owned
`/etc/project-paper-observability/mini-production/publisher.env`. To restore all
prior scheduler/helper files, use the printed rollback script while holding the
same deployment/collector locks. Keys, telemetry and pending state are retained.
No existing paper discovery cron is modified.

Developer verification: shell behavioral tests exercise order, failure
propagation, no collection rerun and the disabled flag, using isolated path/CLI
fixtures. The exact root installer and rollback were also executed in a
network-none disposable Linux container with synthetic config/key and a fake
Docker CLI. Native acceptance must separately observe the next natural cron
run, fresh Mini observed_at and matching authenticated AWS state/receipt. No
forced pipeline run is a substitute for that scheduled evidence.

## Release package integration

The standalone observability release archive includes `collect-publish.sh`,
`install_collect_publish.sh`, and the target-checked `install_package.sh`
entrypoint alongside the existing collector helper. Release CI runs the pipeline
behavioral tests and checks archive contents, source hashes and target rejection.
The entrypoint updates the existing Mini cron; it does not add a schedule or
change application cron jobs. AWS cannot invoke this Mini publication installer.

**General host-code upgrade boundary:** the passwordless helper accepts
only a collector image and target. It does not install archive contents. Adding
files to an archive therefore does not activate this change on an existing host.
The protected deployment must first gain an explicitly reviewed package-install
capability through host provisioning; do not execute arbitrary downloaded scripts
via sudo or broaden the runner's sudo permissions. No such capability is installed
by this change. After the wrapper/helper are provisioned, ordinary image releases
retain collect-then-publish automatically with no manual cron edits. Credentials
are neither requested nor transmitted by the package.

## Production reconciliation: 2026-10-26

Read-only inspection at 14:01 PDT found that the reviewed installation had
already been applied at 13:54:11 PDT. The installer actor has not yet been
established; Architect was asked to reconcile that audit record. Do not repeat
installation based on the earlier collection-only handoff.

Installed root-owned 0755 wrapper SHA256:
`effd533529bb0f0db5fadfd212a88079ac36c43b08f068861c1bba68772ff20d`.
Installed helper SHA256:
`fcb7b7c60a45f5b64b4294468c6b667a8e571612bbdf209d2109e8ce447ff0db`.
The existing root cron invokes the combined wrapper every 15 minutes under the
collector lock. No additional cron or broader sudo permission is required for
this installed flow. Future image deployments through the existing helper retain
that combined launcher.

The natural 14:00 PDT run produced Mini observed_at
`2026-10-26T21:00:07.135024+00:00`; AWS stored the matching observation at
`2026-10-26T21:00:15.501858+00:00`. Receipt count increased from one to two.
Stored and independently calculated canonical payload SHA256 both equaled
`9c49b9b8a845684a3a4d5555f240fefe20bf7d7f2987362007ec4c8430aec430`.
Producer and status environment were both mini-production. These checks made no
host changes and did not force collection or publication.

Four additional disposable Linux tests execute the fixed deployment helper:
repeated Mini image upgrades retain the pipeline; AWS keeps collector-only;
collection-only installations retain that behavior; writable pipeline wrappers
are rejected. Qualification uses a synthetic collector and never invokes the
publisher. Tests use temporary container filesystems, with no production mounts.
General future host-code upgrades remain a separate capability; successful image
updates do not imply arbitrary package scripts can execute as root.
