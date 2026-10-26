# Mini telemetry in AWS Health

The Infrastructure tab reads only the AWS-local received telemetry snapshot via
`observability_transport.health_status`. It uses `PAPER_OBSERVABILITY_STATE_FILE`
(default `/var/lib/project-paper-observability-ingest/mini-state.json`), already
mounted for the ingest service. Rendering creates no files or locks, reads no
transport keys, and makes no network calls to Mini.

The Mini panel shows observation and AWS receipt times, fresh/stale/unavailable,
15-minute CPU average and maximum observed, average/minimum available memory,
latest disk usage/available/capacity, collection coverage and last accepted
publication. Zero measurements are valid. Warming collection is distinct from a
complete window. No publisher failure is inferred from a receipt: later attempts
that never reached AWS are unknown.

Either observation or receipt older than 45 minutes makes the view stale; the
last timestamps remain visible but resource values become placeholders. Missing,
unreadable, invalid or future-dated state also produces placeholders and generic
errors without paths, payload fragments or secrets. A recent receipt cannot make
an old observation fresh. Mini collector/publisher, paper sync and AWS resource
collection are unchanged. This branch requires Architect review before deploy.
