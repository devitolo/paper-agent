# Observability transport: backend preview and manual rollout

Status: implementation preview, flags off by default. Nothing here installs cron,
provisions a key, changes mounts or enables a production endpoint. This package
contains no UI changes. The read-only backend accessor exists for a future
reviewed Health integration; the existing Health page does not consume it yet.
The collector stays network-none. Paper sync and its credentials are unrelated.

## Files and entry points

AWS main application code provides `POST /observability/v1/mini-status` through
its existing handler, delegating to `paper_agents.observability_transport`.
It accepts a maximum of 32 KiB and a dedicated HMAC signature, stores one
sanitized latest-state envelope plus at most 256 receipts, and rejects an older
state or conflicting ID. State and receipts are committed in one locked atomic
file; no paper database or separate projection-file write is used.

`paper_agents.observability_publisher` is a one-shot CLI for a **separate**
container. `deploy/observability/Dockerfile.publisher` packages only the stdlib
transport modules; it does not contain app/SQLite/PDF/pipeline code. No image has
been published by this task. A release must provide a reviewed immutable AMD64
base image and record the resulting source/image digest. The runtime needs only
network egress to approved HTTPS, a sanitized export read-only, its dedicated
transport key read-only, and private pending-state storage writable. Use UID/GID
10002, read-only root filesystem, all capabilities dropped, no-new-privileges,
bounded CPU/memory/pids and temporary storage. Actual host mount paths,
image digest, group ownership, DNS/TLS and resource budget require rollout review.
No Docker socket, application volumes, host proc/sys, raw samples, or discovery
credentials may be mounted in this publisher. Do not modify the collector mounts.

## Manual enable sequence (requires reviewed production rollout)

1. Confirm both clocks, approved private HTTPS URL/TLS certificate chain,
   AWS app identity, and a new persistent ingest directory. No hostname is
   assumed here. The URL path must be exactly `/observability/v1/mini-status`;
   no query, user info, fragments or redirects are supported.
2. Provision a **new independent** random 32-byte key as 64 lowercase hex
   characters, through the approved secret provisioning channel. Proposed files
   are `/etc/project-paper-observability/mini-production/transport.key` and
   `/etc/project-paper-observability/aws-production/mini-transport.key`.
   Never reuse paper sync/Gemini/provider secrets or print the key. Host files
   must be root-owned 0400/0440; give only the intended container identity read
   access through a dedicated group or secret mount.
3. Deploy reviewed AWS application code with these proposed settings:
   `PAPER_OBSERVABILITY_INGEST_ENABLED=0`,
   `PAPER_OBSERVABILITY_INGEST_KEY_FILE` pointing at its dedicated mounted key,
   and `PAPER_OBSERVABILITY_STATE_FILE` pointing to
   `/var/lib/project-paper-observability-ingest/mini-state.json` in a dedicated
   persistent directory writable by the ingest process. Preserve existing Paper
   volumes/configuration/secrets. Check disabled POST returns 404 without writes.
4. Enable only AWS ingest (`PAPER_OBSERVABILITY_INGEST_ENABLED=1`) through the
   reviewed app release/configuration path. Verify authenticated/malformed/oversize
   responses using synthetic nonproduction data first. Unauthenticated requests
   must not create state. Do not expose additional public ingress for this task.
5. Deploy the separately built publisher with
   `PAPER_OBSERVABILITY_PUBLISH_ENABLED=0`. Its CLI requires `--status-file`,
   `--state-file`, `--url` and `--key-file`. The disabled path does not read any
   file/key or contact the endpoint. Use private state such as
   `/state/pending.json`; do not put this state in the read-only export mount.
6. Enable the publisher flag and run once through the approved container job
   path. The payload is an allowlisted projection of the collector export.
   Unknown source fields are dropped; unknown received fields are rejected.
   Both receiver and sender validate timestamps and values. Check a successful
   receipt, accepted target identity and independently advancing observed_at.
   A warming or stale resource observation is not made current by delivery.
7. If scheduled publication is authorized, install **a dedicated publisher** cron
   entry separately from discovery and collection (for example two minutes after
   each 15-minute collection, after actual ordering has been verified). Wrap the
   approved fixed container command with a dedicated lock and a hard outer
   timeout. Do not add Docker access or overwrite existing collector/main cron.
   This task does not install that entry or create a host privileged helper.

Optional `--events-file` reads a bounded JSON array of at most 20 typed events
with only UUID, UTC timestamp, source, state and enumerated code. No raw log,
exception, message, command, path or arbitrary metadata is supported. There is
currently no automatic adapter from paper job logs: omit the option for an empty
array. Adding a job-event producer is separate integration work; do not claim
job/error reporting is connected from the optional input alone.

## Delivery failures and rollback

On network/5xx failure the sender tries at most three times with one/two-second
backoff, signs fresh request timestamps, and retains the **same** exact body and
ID in its private state. HTTPS verification remains on, environment proxying and
redirects are disabled. A 4xx stops immediately and preserves pending evidence.
An accepted but lost response can be retried without replacing newer telemetry.
Pending data older than 45 minutes is rejected: reconcile the sanitized failure
and archive/remove the private pending file through the reviewed operator path
before collecting a new delivery; never rerun discovery to repair telemetry.
Publisher diagnostics contain only state/ID, never keys, payloads or headers.

Disable publisher first (`PAPER_OBSERVABILITY_PUBLISH_ENABLED=0`), then disable
AWS ingest (`PAPER_OBSERVABILITY_INGEST_ENABLED=0`). Turning off AWS ingest returns
404 without writes. Retain the dedicated state directory, keys and prior image
identities for rollback. Roll back application/publisher images through their
normal approved release mechanisms; never replace or restore Paper SQLite data.
If atomic replacement fails, the prior committed state remains; if a reply is
lost after commit, idempotent retry returns a duplicate receipt. Corrupt stored
metadata fails closed and is retained for inspection. Key rotation requires a
coordinated pause/update/resume; this preview supports one key, not dual-key
rotation. No production transport activation or UI integration is included.

## Local smoke and limits

Run `python3 -m unittest tests.test_observability_transport -v` with permission to
bind loopback sockets and `openssl` available. The test creates an ephemeral
self-signed TLS server and trust store, uses the real serving handler, publishes
sanitized data with HMAC, verifies persisted state, checks auth/type/size failures,
and disables ingest. It asserts no Paper DB file is created. Additional tests
cover concurrent delivery, replay/out-of-order conflicts, bounded receipts,
atomic failure recovery, preserved sender retries and read-only freshness.

These are developer tests, not independent QA or evidence of production TLS,
mounts, native publisher scheduling, active job-event inputs, or Health browser
visibility. A later rollout must verify actual artifact/identity/permissions,
resource budget and end-to-end live freshness before claiming production delivery.
