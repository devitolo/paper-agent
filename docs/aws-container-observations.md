# AWS container observations

The existing AWS 15-minute collector launcher gathers a fixed inventory:
`project-paper-prod-app-1` and the scheduled observability collector. Bounded
Docker CLI queries select state, health, image identity, CPU and memory. The
network-isolated observability image sanitizes stdin and atomically writes
`container-status.json` to the existing AWS export directory as UID 10001.
The writer mounts only that directory. It receives no Docker socket or secrets.

Health reads this file through the existing read-only export mount. Its schema,
AWS identity, exact service inventory, UTC time and numeric bounds are validated.
Readings older than 45 minutes, more than five minutes in the future, or malformed
exports are hidden.
Exporter failure preserves host collection and the prior export; its own
observation time controls expiry. Missing containers are reported as unavailable. Mini delivery and its inventory remain separate.

## Activation and rollback

Build the AMD64 observability image using the normal AWS observability release
workflow. The image must contain `paper_agents.aws_container_status`. First deploy
that image through the existing protected image-only helper. Then replace the
existing root-owned collector launcher with the reviewed `run-container.sh` under
the existing deployment and collection locks, saving the previous bytes and hash.
Check the new image's module entrypoint with `--help` in an isolated container
before replacing the launcher. This one-time launcher change uses existing AWS
maintenance privileges; no new cron or sudo permission is required. Preserve the
current config, export, state, logs and rollback image.

For rollback, restore the saved launcher before selecting the prior collector
image. Keep the last export for diagnosis; Health will hide it after expiry.
Verify the serving app remains ready and inspect the next scheduled export and
both container panels. The AWS collector's last successful collection can be shown
even while its short-lived container is absent.
