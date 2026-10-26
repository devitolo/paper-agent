# Mini to AWS observability transport contract (proposed)

Status: design review required before production activation. This is separate
from paper routine sync, discovery, the application SQLite database, and the
collector's network-isolated container. The network-isolated collector remains
unchanged. A separate bounded publisher reads its sanitized export.

Implementation scope update: the backend publisher/HTTP ingest/atomic storage
preview is implemented behind default-off flags. Per the current coordination
scope, UI integration and independent tester review are deferred. The Health
section below is the proposed consumer contract, not an enabled application
feature. See observability-transport-operations.md for manual rollout gates and
current limits, including the optional event-file input without a job adapter.

## Endpoint and activation

AWS exposes `POST /observability/v1/mini-status` only when
`PAPER_OBSERVABILITY_INGEST_ENABLED=1`. Disabled means HTTP 404, no writes and no
secret reads. Query strings, compressed bodies, transfer encoding, missing or
conflicting Content-Length, and content types other than application/json are
rejected. Maximum body is 32 KiB; read timeout is five seconds. The endpoint
accepts only producer `mini-production`, never caller-supplied paths/targets.
Use the existing approved private HTTPS serving URL; its actual hostname and
TLS arrangement must be verified during rollout rather than invented here.
No public ingress change is part of this implementation.

## Dedicated authentication

Use a new independent 32-byte random HMAC key represented by 64 lowercase hex
characters. Proposed host files:

- Mini: `/etc/project-paper-observability/mini-production/transport.key`
- AWS: `/etc/project-paper-observability/aws-production/mini-transport.key`

Provisioning and rotation require reviewed deployment changes. Host files are
root-owned and readable only by their dedicated publisher/ingest identity
(0400 or 0440 with a dedicated group). Containers see only a read-only mount of
their dedicated key. Never reuse or read paper sync, Gemini or provider secrets.
No key is generated, provisioned, logged or copied during contract preparation.

Headers: `X-Paper-Observability-Timestamp` (integer Unix seconds),
`X-Paper-Observability-Id` (lowercase UUID), and
`X-Paper-Observability-Signature` (64 lowercase hex). HMAC-SHA256 signs the UTF-8
string `POST\n/observability/v1/mini-status\nTIMESTAMP\nID\nSHA256_BODY` where
SHA256_BODY is the lowercase SHA256 of exact request bytes. Verify with a
constant-time comparison before parsing/storing. Request timestamp must be
within 300 seconds of AWS clock. Headers are never included in diagnostics.

## Payload and sanitization

Envelope has exactly `schema`, `producer`, `status`, `events`. Schema is
`project-paper.observability-delivery.v1`; producer is `mini-production`.
`status` is a *projection* of the collector's status.json, never arbitrary
pass-through JSON. It has:

- `schema=project-paper.resource-status.v1`, `host=mini-production`,
  `environment=mini-production`, `scope=host`, `state=warming|observed`,
  `observed_at` (UTC RFC3339), `collector_version` (bounded numeric string).
- `window`: UTC start/end, interval_seconds=900, complete (boolean),
  sample_gaps (nonnegative integer). End minus start is exactly 900 seconds.
- `sample_count` (0..100), `coverage_seconds` (0..900).
- `cpu`: nullable finite 0..100 average_percent, max_observed_percent;
  legacy one-minute/five-minute peak fields are null (no fabricated peaks).
- `memory`: nullable nonnegative bounded integer available_average_bytes,
  available_max_bytes, available_min_bytes (point observations, not continuous
  used-memory peaks).
- `latest`: nullable observation time, nullable `disk`, and enumerated
  container/temperature/cgroup availability states only. Disk contains nullable
  capacity_bytes/available_bytes (nonnegative bounded integer), used_percent
  and inode_used_percent (finite 0..100). No filesystem path is sent.

Unknown keys, duplicate JSON keys, nested free-form objects, NaN/Infinity,
booleans as numbers, invalid UTC timestamps, bad ranges and contradictory
counts/windows reject the entire envelope (400). No source paths, raw samples,
boot IDs, counters, environment dumps, exception text or application data are
sent. Strict projections apply on sender and receiver, independently.

`events` contains at most 20 recent sanitized events, each with exactly `id`
(UUID), `observed_at`, `source` (arxiv|openalex|semantic|core|ssrn|zenml|
collector|publisher), `state` (started|completed|failed|skipped), and `code`
(from a closed reviewed enum such as none|timeout|unavailable|rate_limited|
validation_failed|delivery_failed|overlap). No message/log field. Events are
latest-state troubleshooting context, not a reliable history or alert stream.
Absent event evidence means an empty array. A job-event producer adapter and
any event scheduling require explicit tests; do not manufacture events.

## Freshness, ordering and retry

New delivery status may be at most 45 minutes old and 300 seconds in the future.
Event times must be no later than request clock+300s and no older than seven
days. Receiver ordering uses resource observed_at plus exact body hash:

- Same delivery ID and body hash: 200 acknowledged duplicate (no rewrite).
- Same delivery ID and different hash: 409 conflict.
- Older status: 409, never replace newer telemetry.
- Same observed_at with different body: 409; same body is idempotent.
- Newer valid authenticated status: atomic commit then 200 receipt.

Persist ordering/idempotency metadata with status in one file under the same
lock. Keep last 256 accepted IDs/hashes; evicted requests remain prevented from
regressing state by observed_at ordering. Publisher retries exact bytes/ID with
a fresh request timestamp/signature. Use bounded networking (5s connect/read,
no redirects, max three attempts with bounded backoff), no discovery rerun.
A 409 keeps local evidence and reports a sanitized delivery_failed code.

Health computes age from resource observed_at (never received_at), labels
warming separately, and marks stale after 45 minutes even if delivery retries
succeed. Missing/unreadable/malformed data is explicit; never substitute zeros.
Publisher delivery failure cannot claim the collector failed.

## Storage and read-only Health

Proposed AWS container directory: `/var/lib/project-paper-observability-ingest`.
Dedicated persistent volume/bind configuration is a reviewed deployment step.
Store `mini-state.json` (envelope plus received_at, latest body hash, bounded
receipt metadata), mode 0640, and a dedicated lock file. No SQLite tables and no
shared application DB writes. Hold an exclusive flock across read/compare/write;
write a same-directory temporary file, fsync, atomic replace, then fsync parent.
Crash before replace preserves old state; crash after replace can be safely
retried. Strictly validate persisted state before using it; corrupt storage
fails closed without overwriting evidence. Response receipt is bounded and
contains only schema, delivery ID, accepted resource timestamp and duplicate.

Health reads the same committed envelope through a dedicated bounded parser and
extracts only sanitized `status`/`events`. No second status-file write, avoiding
two-file consistency bugs. Existing local/AWS collector file reads continue;
this receiver supplies only the Mini panel when the ingest flag is enabled.
The Health request path never sends telemetry or mutates storage.

## Rollback and release boundaries

Flags are independent: AWS ingest and Mini publisher each default off. Turning
off publisher stops network transmission; turning off ingest returns 404 and
Health reverts to its existing configured source. Preserve state, keys and
receipts for rollback. No cron or main-app deployment may overwrite standalone
observability cron. Collector image remains network-none and has no key mount.
A separate least-privileged publisher/container needs the export read-only,
private retry state writable, dedicated key read-only, and approved outbound
HTTPS; no Docker socket, host proc/sys, SQLite, PDFs or business volumes.

Required tests: HTTP auth/length/timeouts; schema/range/type/secret rejection;
replay/conflict/out-of-order/concurrent ingest; atomic write/failure recovery;
size and retention bounds; sender exact retries/redirect rejection; Health
zero/unknown/warming/stale; enabled/disabled gates; full synthetic publisher to
HTTP receiver to committed state to Health. Production acceptance additionally
verifies mount/key ownership, TLS reachability, source identity, actual delivery
and independently advancing resource timestamps. No production changes occur
until this contract and deployment details are reviewed.
