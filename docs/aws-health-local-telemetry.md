# AWS-local collector export in Health

The serving release attaches the existing
`/var/lib/project-paper-observability/aws-production-export` directory read-only
at the same container path. `PAPER_AGENT_AWS_RESOURCE_STATUS_FILE` selects its
status.json. No collector, transport, Mini or secret configuration is changed.

A narrow JSON Compose override contains only that bind mount and environment
path. The base Compose file and its image variable, existing data/config/backup/
log/key mounts remain intact. Subsequent serving releases use the override.
Before first attachment, the existing SQLite backup API writes a consistent
backup to the established backups volume. Release rollback restores the prior
environment, Compose and override (or removes a newly introduced override).
Application health is verified through the normal serving deployment path.

The backend bounds reads to32KiB and projects only AWS-attributed identity,
window/observation freshness and numeric CPU/memory/disk metrics. Source paths,
free text and other raw fields are never rendered. Zero readings are valid.
Missing, malformed or future snapshots show placeholders. Observations older
than45minutes are stale; disk uses its own observation timestamp and is hidden
when stale even if CPU is fresh. Mini readings remain on the authenticated
received-state accessor. Paper-data delivery state is not presented as an AWS
CPU/resource fact.

Tests include actual Compose merge preservation, safe/freshness rendering and
isolated Linux release success/health-failure rollback with synthetic Docker.
No host builds or collection jobs are introduced by this serving change.
