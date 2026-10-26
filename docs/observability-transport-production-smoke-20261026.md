# Controlled Mini-to-AWS transport acceptance, 26 October 2026

User authorized production transport enablement through Architect. UI and tester
work were explicitly excluded from this phase. One unique sanitized Mini status
delivery and one exact duplicate replay were exercised; there is no recurring
publisher cron or persistent publisher service yet. AWS ingest remains enabled.

## Release identity

- Tested backend: `163bac34efc39be4f31ba0fbd5b147677ce2451e`.
- Narrow AWS integration PR: https://github.com/devitolo/paper-agent/pull/11
- AWS release SHA: `9e3ed5004ba4a5717d987a530e9e4097ad7d9b1e`.
- AWS build/deploy: https://github.com/devitolo/paper-agent/actions/runs/40395745388 (success).
- Serving image: `ghcr.io/devitolo/paper-agent@sha256:9c35c8957b13b81c79fff0e259f55d6da97b82f380ead16c3a2623fe3c95cfa7`.
- Publisher build: https://github.com/devitolo/paper-agent/actions/runs/40395946334 (success).
- Publisher source: `99508495e58dacbf342e11c1c16529e56fe76cc6`.
- Publisher image: `ghcr.io/devitolo/paper-agent/observability-publisher@sha256:7fd64eaeaffce81f259f7fbe6abd61022fe359e9503e89fd57a936885bbc8055`.
- Publisher inspected as linux/amd64, UID/GID 10002. Build used an immutable
  Python base digest and published provenance/SBOM on GitHub-hosted runners.

## Provisioning and enablement

The user ran the staged Mini bootstrap with local sudo. It generated a dedicated
transport key without printing it, root:10002 mode 0440, and publisher state owned
10002 mode 0700. Pairing was transferred in memory through encrypted SSH, using
only the approved non-root key mount with Docker logging disabled. Existing AWS
keys were not reused. The AWS transport key is root:10001 mode 0440. No key bytes
or hashes are included in this evidence.

Reviewed AWS enable script: `scripts/observability_transport_aws_enable.sh`,
SHA256 `e889bcbf3460a9bf828ffe42991d2977c9b28ce7a241e2cafbac1bffa8d50543`.
It held the existing deployment lock, retained prior Compose/env configuration,
created a consistent SQLite backup in the existing AWS backup volume, and
recreated only the existing app with `--no-build --pull never --no-deps`.
No SQLite content entered telemetry or crossed hosts. Existing data/config/log/
backup/control/Gemini mounts were independently rechecked and preserved.

AWS enabled settings:

- `PAPER_OBSERVABILITY_INGEST_ENABLED=1`
- `PAPER_OBSERVABILITY_INGEST_KEY_FILE=/run/secrets/paper_observability_transport_key`
- `PAPER_OBSERVABILITY_STATE_FILE=/var/lib/project-paper-observability-ingest/mini-state.json`

Dedicated state directory and read-only key file were the only added mounts.
Before enablement the endpoint returned 404. AWS readiness is healthy afterward.

Existing verified route (no new ingress):
`https://paper-prod.tail5fc461.ts.net/observability/v1/mini-status` through the
existing tailnet-only HTTPS proxy to the app on loopback port 8000.

The first publisher attempt failed because Docker's default DNS did not resolve
the private Tailscale name. It retained its exact pending body and ID; AWS had no
state file. Mini's configured Tailscale DNS was verified as `100.100.100.100`.
A credential-free container probe with that resolver verified both private DNS
and certificate-validated HTTPS /ready=200. The publisher retry used this explicit
resolver. No TLS verification bypass, host networking, or proxy was introduced.

The one-shot publisher ran with flag=1, UID/GID 10002, read-only root filesystem,
all capabilities dropped, no-new-privileges, 64 MiB memory/swap limit, 0.1 CPU,
16 PID limit, bounded tmpfs, private DNS, approved read-only sanitized export/key
mounts and private writable retry state. No Docker socket, raw samples, SQLite,
PDFs, application volumes or pipeline credentials were mounted. An outer timeout
and container cleanup guarded the controlled invocation.

## Observed data flow

- Delivery ID: `a61f1b9d-c369-4d12-9498-c2924779d6f2`.
- Resource observed: `2026-10-26T20:30:06.874113+00:00` (1:30:06 PM PDT).
- AWS accepted: `2026-10-26T20:44:09.461769+00:00` (1:44:09 PM PDT).
- Body: **1016 bytes**, strict allowlist validated, producer `mini-production`.
- Complete 900-second coverage, zero gaps, CPU average **2.27%**, disk **15.66%**.
- Events: empty array; automatic job/error event collection is not connected.
- AWS `mini-state.json` mode 0640; one matching ID/body-hash receipt.
- Independently recomputed payload hash matched latest-state and receipt hashes.
- Directory contained only `mini-state.json` and its dedicated lock; no leftover
  temporary files. State was written by the tested fsync/atomic-replace backend.
- Exact replay from Mini over HTTPS returned HTTP 200, `duplicate: true`, same ID
  and resource timestamp. AWS received_at and receipt count remained unchanged.
- Successful sender removed pending.json; no controlled publisher remained.

This confirms one authenticated production data flow and safe duplicate handling.
It does not establish continuous publication, event adapters, UI consumption,
live browser visibility, or independent production QA. The resource timestamp
remains the freshness authority; a delivered old sample is not relabeled current.

## Disable and rollback

There is no publisher cron to remove; the controlled publisher container exited.
Future publisher runs can be disabled with flag=0. To disable AWS ingest and
restore its exact prior Compose/env configuration, run the retained root script:

```sh
sudo bash /home/ubuntu/project-paper-prod/production-releases/observability-transport-1vJ5D3Ud/rollback.sh
```

Review its saved configuration first and serialize with the AWS deployment lock
if any later release has occurred. It recreates only app from the existing image
and mounts. It does not restore or delete SQLite, telemetry state, or keys. The
same release directory retains `compose.before`, `env.before`, `image.before`,
`backup-evidence`, and enablement `result` for review. No rollback was required
or executed during this successful smoke. Returning to the earlier serving
binary is a separate normal image rollback; this script disables the feature.

Further steps require explicit scheduling and UI integration decisions. Do not
claim the AWS Health page displays Mini metrics from this backend-only smoke.
