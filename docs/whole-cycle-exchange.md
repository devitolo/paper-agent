# Daily Mini exchange (implementation awaiting production review)

The default remains off. Set `PAPER_AGENT_ROUTINE_SYNC=1` only after the routine
contract has been qualified; otherwise the legacy SQLite recovery pull remains in
use. No cron installation or deployment is part of this change.
The host wrapper runs `mini_sync_exchange.sh pull`, the existing OpenAlex, arXiv,
Semantic Scholar, CORE and profile jobs, then `mini_sync_exchange.sh push` when
both explicit activation flags are provisioned. The helper uses the same Compose
files and `exec -T app` as `mini_container_job.sh`. Python runs in the app image
against `/app/data/paper_agent.db`, `/app/config`, and `/app/data/sync-state`.
The HMAC secret must be provisioned privately in the app environment through the
existing Compose env file. It is never placed in host command arguments. The
host requires an HTTPS origin, `PAPER_AGENT_SYNC_ENABLED=1` and
`PAPER_AGENT_SYNC_EXPLICIT_PRODUCTION=1`. The CLI enforces activation independently.
The wrapper holds the shared deploy lock throughout an enabled cycle. Each
exchange operation takes the existing runtime and exclusive scheduled-job lease.
The host helper passes the routine flag explicitly into the app container, so a
host setting cannot be mistaken for an app setting. Enabling routine exchange is
a separate reviewed environment change and does not alter cron timing or job
ownership.

## Restore ownership

`GET /sync/snapshot` with `Accept: application/gzip` streams the existing consistent
SQLite backup package, including the complete serving database, topics, industry
configuration, active profile and summary files. TLS and timestamped HMAC protect
the transfer; the existing bounded extraction and checksum/SQLite/FK checks apply.
The logical JSON endpoint remains for old isolated workers.

AWS owns the restored serving tables, configuration and profile. Before replacing
the working DB, Mini saves a consistent `mini-before-<batch>.db` for rollback.
Only Mini's four `*_search_state` tables are copied into the staged restored DB.
Page dispositions reference old scout IDs: they remain available in that saved
Mini database rather than being inserted with invalid FKs into the AWS snapshot.
PDFs, model files, credentials, runtime identity, logs, and other Mini files are
not replaced. SQLite backup into the existing destination connection commits DB
replacement through SQLite, coordinating WAL and readers without unlinking a live
DB inode. Serving files are installed before the DB; if installation or restore
fails, the durable `restoring` marker blocks a new cycle for operator recovery.
This is fail-closed recovery, not an atomic cross-filesystem directory swap.

## Results and recovery

One UUID and baseline are saved after restore. New cycles identify the relevant
scout candidates and evaluations, including discovered unscored papers and updated
existing papers. The bounded payload carries full paper fields, source identities,
portable triage-summary JSON, all scores, and the ordered recommendation subset.
Batch-scoped cycle/run keys preserve distinct curator attempts. Peer numeric IDs
and Mini artifact paths are not sent. Profile-stage edits are excluded; the
separate profile-proposal path is not called or enabled by this adapter.

AWS validates dependencies, duplicate scores/orders and summary content before
writes, maps canonical identities to its own FKs and commits the batch/receipt
with the pipeline rows. Conflicts abort rather than silently discarding scores or
recommendations. Feedback, topics, profiles, saved/excluded decisions and secrets
are never written by this upload. Content-addressed summary files are written
under the AWS app root. A filesystem failure aborts SQLite; an unreferenced
content-addressed file can remain if commit fails and is safe to reuse.

`pipeline-outbox.json` is atomically persisted and fsynced before POST. A lost
response retries the exact UUID, sequence and payload, and AWS returns the original
receipt. Pending outbox or unfinished cycle blocks the next pull before download.
Retry `push` to recover a lost response. After a pipeline interruption, inspect the
active-cycle and stage logs and complete/recover the stages explicitly; inference
is never automatically repeated. Restore failures require using the saved DB and
validated snapshot to recover deliberately. Snapshot archives, prior Mini DBs,
published payloads and receipts are retained; establish a reviewed retention policy
before activation. No automatic deletion of production backups is introduced.

## Evidence and limits

`tests/sync_exchange/test_whole_cycle.py` uses real HTTPS with an ephemeral trusted
certificate and the actual wrapper, shell helper, argparse CLI, compressed backup,
SQLite restore, result exporter and AWS handler. Docker execution is replaced by
a temporary launcher that maps `/app` to the test volume and simulates five stages.
It verifies grouping, unscored discoveries, updates, summaries, scores,
recommendations, feedback added after pull, failed SQL rollback, lost-response
replay, pending-pull refusal, cursor preservation and activation guards.

These tests do not run a real Docker daemon or production images. SRE must qualify
the image, volume permissions, runtime health/import-manifest behavior, credentials,
resource use and retention before production activation. No production runner,
cron, AWS data or Mini database was accessed by the regression harness.
