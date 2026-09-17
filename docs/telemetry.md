# Optional local Phoenix tracing — first experiment

Tracing is disabled unless `PAPER_AGENT_TELEMETRY=1`. The native application
does not require Docker or OpenTelemetry. Optional dependencies are pinned in
`requirements-telemetry.txt`; install them into the Python environment that
actually runs the pipeline only after approving enablement. Missing dependencies
or malformed configuration disable tracing without preventing pipeline work.

The implementation uses the OpenTelemetry Python API/SDK and its standard OTLP
protobuf encoder, with a centralized OpenInference attribute mapping. No global
provider or auto-instrumentation is installed. The small HTTP transport sends
protobuf to `/v1/traces` with `application/x-protobuf`; it avoids exporter retries,
environment-provided headers/proxies, response-body logging, and implicit SDK
exit handlers. It does not follow redirects. An explicit HTTP/HTTPS endpoint is
supported; credentials, query strings, fragments, and alternate paths are rejected.
Default: `http://127.0.0.1:6006/v1/traces`. No Collector is needed.

## Trace contents

Each enabled automated `run_daily_pipeline` invocation has one independent root.
Scout attempts and source HTTP attempts, Curator and per-candidate evaluations,
Reviewer/per-paper extraction, chunk/synthesis operations, and actual Ollama
requests are nested below it. Each threadpool submission copies the parent
context. Scout is deterministic and never labeled as an LLM. Retry, rescout,
abstract fallback, deterministic merge fallback, and recommendation-ID events
describe actions the application actually took.

Allowlisted metadata includes numeric database IDs/counts, attempt/chunk indexes,
HTTP codes, fixed outcome/error categories, model identifiers, existing evidence
prompt and scoring version identifiers, and provider-reported prompt/completion
token counts. Zero tokens are retained only when actually reported. Missing
usage, cost, and unversioned extraction prompts are omitted. Curator IDs are
created after evaluation, so correlation follows the Curator parent; recommendation
events attach generated recommendation IDs without changing application results.
Manual discussion and Feedback traces are not included in this slice.

No paper titles/text, prompts, responses, feedback, paths, URLs, credentials,
subprocess arguments, raw exception messages/stacks, user/host resource metadata,
or arbitrary dictionaries are exported. No content-capture switch is provided.
Spans have fixed names, at most 32 attributes, 16 events, 8 attributes/event, and
string identifiers at most 128 characters. IDs are span attributes, not metrics.
Phoenix may consequently show empty input/output panels; that is intentional.

## Bounds and failure behavior

- All enabled invocations are sampled; disabled invocations create no exporter.
- One cached backend/daemon worker per process, queue 256 spans plus one batch of
  at most 32 in flight. Overflow drops newest spans. No unbounded worker creation.
- HTTP socket timeout 1 second, no application retry. This is **not** a total
  network wall-time deadline: DNS or slow-drip headers can exceed it.
- End/enqueue the root before draining. Pipeline completion waits at most 0.25 s
  for queued/in-flight export (plus ordinary scheduling/CPU overhead). Explicit
  processor shutdown joins at most 0.25 s. No synchronous provider shutdown/atexit
  join is registered. Export cannot change pipeline return values, DB writes,
  ranking, or application exceptions.
- Pending spans remain queued while a living process drains. Short-lived jobs
  can lose pending spans at exit. Full queues, backend outages, and dropped roots
  can produce partial traces; tracing is best effort, not an audit log.
  Missing spans are not proof that an activity did not occur. Do not use this
  pilot's traces as availability or quality denominators. Safe local pending,
  dropped-span, and export-failure counters appear in the synthetic probe report;
  no product UI or blocking diagnostic writes are added.
- Ordinary failed requests are dropped and later batches can recover. A worker
  stuck inside DNS/transport cannot deliver again until that call returns or the
  process restarts. The queue/caller wait remain bounded. There is no claimed
  automatic recovery from an indefinitely stuck transport. Endpoint configuration
  is read on first successful initialization; restart the process to change it.

## Hermetic tests and separately approved Mini smoke

SDK tests use `python -m unittest discover -s tests -p 'test_telemetry.py' -v`
in an environment with the optional requirements. The tests exercise in-memory
traces, actual OTLP encoding against a local HTTP stub, source retry framing,
thread parentage, usage/privacy, failure isolation, queue saturation, and pipeline
output/database comparisons. Independent acceptance is tracked in
`qa/phoenix_acceptance.md`. Hermetic passing tests are not Mini/Phoenix clearance.

Development-host overhead sample (macOS, Python 3.14, 20 synthetic invocations,
31 spans/invocation, in-memory exporter): disabled median 0.017 ms/max 0.036 ms;
enabled median 6.621 ms/max 6.705 ms, including the bounded flush. This measures
instrumentation around empty operations, not model/source throughput or Mini
performance. A blocked-exporter fixture adds the configured 0.25 s drain wait
per invocation; OS scheduling can add overhead. These are observations, not a
zero-overhead or real-time guarantee.

Before live integration, separately verify Phoenix's actual image/version,
loopback binding, persistent volume, retention, analytics, and external-resource
settings. The proposed 14-day retention and privacy configuration have **not**
been verified by this patch. Do not expose an unauthenticated listener publicly.

After approval, use the pipeline's Python environment to install optional deps:

```bash
python3 -m pip install -r requirements-telemetry.txt
```

Then explicitly run one synthetic trace, with no research data, source/model
requests, or database access:

```bash
PAPER_AGENT_TELEMETRY=1 \
PAPER_AGENT_OTLP_ENDPOINT=http://127.0.0.1:6006/v1/traces \
python3 -m paper_agents.telemetry_smoke
```

`sent` means the HTTP endpoint acknowledged export; confirm the printed trace ID
in Phoenix to establish UI interoperability. `not_confirmed` may mean an outage
or the bounded flush expired, not a pipeline failure. Inspect the synthetic root
and child, fixed resource names, metadata-only attributes, and absent input/output
text. Only after that check should the user separately enable an actual scheduled
pipeline invocation; no cron, service, Docker, or deployment settings are changed
by this patch. Unset `PAPER_AGENT_TELEMETRY` to disable future tracing.

References: [OpenInference specification](https://arize-ai.github.io/openinference/spec/),
[OpenTelemetry Python SDK](https://opentelemetry-python.readthedocs.io/en/stable/sdk/trace.html).
