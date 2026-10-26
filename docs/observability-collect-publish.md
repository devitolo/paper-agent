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
