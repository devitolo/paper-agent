# Health operational evidence

Mini owns scheduled processing, model inference, source cursors and local logs.
AWS serves application data and Health. A missing operational record is not proof
of a failed job or a measured zero.

## Result-batch contract

The existing signed, idempotent result batch can carry an optional `operational`
object, version 1. It contains up to 200 processing events and 200 Scout-run count
records, with an encoded size limit of 64 KiB. Events contain only timestamp, known
job name and status. Scout facts contain original run identity, source, timestamps,
and observed candidate/eligible/excluded counts. Arbitrary messages, provider
responses, host paths, environment values, credentials and raw logs are excluded.
Unknown fields, invalid counts, future timestamps and inconsistent totals are
rejected before committing business data or a receipt.

No SQLite schema migration is required. The validated facts remain in the existing
immutable `sync_result_batches.payload_json`, committed atomically with the batch's
AWS receipt. Replaying an identical batch does not duplicate records. The Health
projection also deduplicates the same retained observation across distinct batches,
and excludes exact native Scout rows already present in the serving database.

Mini collects current-cycle Scout facts even when no papers or recommendations are
exported, plus a bounded seven-day tail of processing events so an unsuccessful
prior job can be reported by a subsequent successful publication. Each cycle records
its start timestamp. SQLite's second-resolution timestamps are handled inclusively.
AWS replies with `operational_version: 1`; a new Mini producer retains its durable
outbox if that acknowledgement is missing. Deploy the AWS receiver before Mini.

## What Health shows

- Jobs reports actual processing observations. **Evidence Delivery** describes AWS
  acceptance of the operational evidence, not success of the processing operation
  or proof that a failed job's paper results were published.
- Mini sync reads AWS batch receipts, independent of chart/source filtering. Older
  batches without operational facts still prove acceptance but do not invent counts.
  Recent batch identities and receipt status remain visible for troubleshooting.
- Scout facts feed source/daily counts with Pacific day boundaries. Native daily
  Health queries use the same Pacific conversion, including DST handling. Imported
  Curator rows fall back to real paper-source identity when no Scout FK exists.
- An observed count of zero remains zero. Missing or partially covered Scout data is
  N/A. Incomplete chart coverage is rendered as an explicit table, not a zero trace.
- Latest Run can use an actual processing observation even for a zero-result run;
  it does not claim that a synthetic workflow row exists. Processing failure remains
  distinct from acceptance of its evidence.
- Host CPU/memory/disk/container telemetry continues through the existing separate
  observability transport. This change does not alter collectors or mounts.

Health reads at most the latest 200 matching receipt batches plus the latest global
receipt. This is bounded operational history, not a replacement for retained source
logs. Missing, disconnected or stale delivery evidence remains unknown; Health
cannot know about an event that has not reached AWS.

## Verification and rollout

Tests cover the real exporter, authenticated local HTTPS receiver, SQLite receipts,
and rendered Health HTML, including zero-result success, processing failure with
accepted evidence, replay/ACK loss, missing-vs-zero metrics, source/range/Pacific
boundaries, and rejection without partial writes or credential exposure.

Release order is AWS then Mini, each through its own protected deployment branch and
immutable AMD64 image workflow. Existing consistent backups, configuration, secrets,
feedback/profile data, cursors, volumes, schedules and rollback are preserved. An old
AWS image may omit the new acknowledgement; Mini then retains its outbox rather than
silently discarding facts. Legacy batches without the new field remain supported.

Mini also has a guarded `publish-health` exchange command for actual retained
seven-day operational facts only. It acquires the existing sync/lifecycle/scheduled
job locks, refuses an active/pending source cycle, obtains a current revision without
restoring a snapshot, and publishes empty business sections. Its durable health outbox
uses the same producer sequence and explicit acknowledgement. Normal `retry` flushes
that health outbox before a scheduled source can pull new state. It never runs
provider discovery or replaces business/configuration data. This is suitable for
postdeployment verification/backfill of retained facts; synthetic production papers
or fabricated telemetry are not necessary.
