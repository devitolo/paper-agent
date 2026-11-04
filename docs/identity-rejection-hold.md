# Held identity-conflict deliveries

This change is prepared locally and has not been deployed. It extends the existing
Mini whole-cycle exchange; AWS identity validation remains unchanged.

Only HTTP 422 with the explicit receiver error `source alias belongs to a different paper`
enters this path. Other failures retain their existing retry/error behavior, including
uncertain delivery after a lost response. The existing unknown-revision handler is unchanged.

At the following source job's retry step, Mini preserves the exact rejected payload,
a consistent post-run SQLite database, the active cycle marker when present, and
all database-referenced artifact files in a content-addressed `sync-state/held/` directory.
A manifest records hashes, original artifact paths, batch ID, input revision, sequence,
`Held / Needs review`, and `automatic_replay: false`. Missing or unsafe artifact paths,
backup failures, or integrity failures stop recovery without discarding the pending payload.
This deliberately favors preservation over continuing when evidence cannot be saved.

The capsule is flushed and committed before a durable restore-required marker is
published and the active retry slot is released. Recovery revalidates file hashes and
can finish after an interruption. The held payload is never sent automatically and is
never recorded as applied. `recovery-status.json` reports `held-needs-review`; this is
local exchange status, not a new AWS Health reporting channel.

Before subsequent discovery, the existing validated `/sync/backup` restore replaces
the contaminated working database once, even when normal routine JSON sync is enabled.
Existing restore checks, SQLite backup, and Mini cursor preservation apply. PDFs,
credentials, runtime identity and logs remain on Mini. Serving configuration/profile
are refreshed from AWS. The failed run remains in the held database. Failed restore
leaves the restore-required marker intact and discovery cannot proceed.

`reserved-sequence.json` tracks assigned publication numbers separately from
`sequence.json`, which still records acknowledged delivery. Both source and Health
publication allocate above the greater counter. Holding sequence 33 reserves it without
claiming success; the next result uses at least 34. Crashes may leave harmless gaps.

A held batch needs explicit later reconciliation against the canonical AWS identity
graph. Once a later sequence is accepted, the original held batch must not be replayed
unchanged; any approved corrected publication needs a new batch identity/sequence and
a supersession record. No reconciliation, automatic expiry, deletion, or repair is
implemented here. Existing held evidence may consume disk; insufficient storage fails
closed. Normal scheduling and existing leases remain unchanged.

Tests cover the real HTTPS rejection/restore/next-source receipt, exact evidence
preservation, strict classification, cursor preservation, sequence allocation, crash
recovery, corrupted evidence, failed backup/artifact copy/restore, uncertain ACK, and
prevention of operational publication from the rejected working state.

## Local validation — November 3, 2026

Command (run from the isolated worktree):

```sh
PYTHONDONTWRITEBYTECODE=1 /tmp/paper-qa-tools/bin/python -B -m unittest \
  tests.sync_exchange.test_identity_hold \
  tests.sync_exchange.test_whole_cycle \
  tests.sync_exchange.test_routine_sync \
  tests.sync_exchange.test_http_serving \
  tests.sync_exchange.test_exchange \
  tests.test_operational_evidence \
  tests.test_core_identity \
  tests.test_mini_container_job -v
```

Result: **79 tests run, 78 passed, 1 skipped**, no failures/errors (16.725 seconds).
The skip is the pre-existing Linux watchdog parent-loss regression, which requires
Linux rehearsal; it is not verified by this macOS run. The new hold module's 17
tests passed, including the real TLS receiver and actual cron shell wrapper with
only Docker/inference substituted by the established isolated fixture. This is not
a live host, production Docker, or production deployment test. Existing resource
cleanup warnings were emitted by the test suite. `git diff --check` passed with no output.

## Independent QA correction — November 3, 2026

Tester reproduced a crash after the durable hold marker was committed but before
recovery removed the pending slot. The original `push()` guard covered only new
outbox creation, so a direct push could retransmit the held pending batch.
The restore-required guard now runs unconditionally at entry to both `push()` and
`deliver_outbox()`, before transport or receipt/sequence updates. The independent
six-case adversarial QA module is included unchanged, plus a direct lower-level
delivery regression. Both assert zero transport calls and unchanged acknowledged
sequence/evidence at this crash boundary.

Final local command adds `tests.sync_exchange.test_identity_hold_adversarial_qa` to
the validation command above: **86 tests run, 85 passed, 1 existing Linux-only skip**,
no failures/errors (17.327 seconds). `git diff --check` passes. Independent Linux and
frozen-AWS continuation verification of this correction is requested; no rollout,
replay, push or production mutation has occurred.
