# Phoenix first-slice QA acceptance

Status: preparation only; no observability implementation or Mini configuration is QA-cleared.
Owner: tester. Application instrumentation belongs to developer.
Do not execute production jobs from this checklist without separate authorization.

## Required implementation contract

Before testing, record exact commit/diff, dependency versions, default enablement,
attribute allowlist and version identifiers, queue capacity, batch size, export
timeout, shutdown deadline, overflow policy, and sampling policy. Numeric bounds
must be specified before results are evaluated, not chosen after measurements.
Record the expected scope of retries, fallbacks, and model/tool spans.

## Hermetic acceptance matrix

Use disposable SQLite databases, synthetic papers and profiles, deterministic
source/model fixtures, and a recording exporter. Never send real private content.

| Scenario | Required assertions |
| --- | --- |
| One successful automated workflow | One root trace; Scout, Curator, and Reviewer/extraction spans share trace ID and correct parent IDs. Stage counts and stable DB IDs match persisted rows, not only return values. Scout has no invented model call. |
| Rescout then recovery | Both Scout attempts and their Curator attempts belong to the same workflow; distinct attempt/run IDs; no duplicate model spans or double-counted recommendations. |
| Concurrent chunks | Force completion out of order with at least two workers; each chunk/model call has the correct extraction parent. Repeat with concurrent workflows to detect context leakage. Output order and content remain unchanged. |
| Retry, fallback, and failures | Inject source 429, model timeout, malformed model JSON, and extraction failure. Attempt counts, source/model identifiers, sanitized error categories, and final workflow outcome agree with actual execution. No fabricated retry/fallback or success. |
| Usage | Supply known token counts, missing fields, explicit zero, and partial usage. Observed counts are exact; absent usage/cost stays absent or explicitly unknown. Parent totals do not double-count child usage. Do not estimate Gemini usage as zero. |
| Privacy | Seed distinct canary strings in prompt, response, paper title/text, profile, written feedback, credential-bearing URL, command arguments, and exception text. Inspect the complete serialized export, including span names, attributes, events, status descriptions, resources, and instrumentation metadata. No canary may occur. Only agreed identifiers and allowlisted metadata are exported. |
| Disabled tracing | Patch networking/export construction to fail if called. Run a full fixture workflow and confirm no telemetry network requests or exporter thread and identical business results. |
| Missing optional dependencies | Use a fresh interpreter with telemetry dependencies unavailable. Normal imports and pipeline execution still succeed under the agreed optional-dependency behavior. |
| Exporter unavailable | Test connection refused, HTTP failure, and a server accepting but never responding. Pipeline results/DB rows remain identical; model/source retry counts do not change. Verify measured time stays within agreed bounds. |
| Queue saturation | Block export and produce more spans than capacity. Producer stays bounded, memory/queue does not grow without bound, and the documented drop policy applies without blocking the pipeline. |
| Shutdown and cancellation | Shutdown/flush under outage finishes within its deadline; no persistent exporter worker or deadlock. Tracing must not weaken existing Gemini child cleanup. |
| Exporter faults | Inject exporter/serialization exceptions during span start/end and shutdown. They do not replace business exceptions, alter recommendations/scores, or cause extra business work. |
| Stable outcome comparison | Compare tracing disabled/enabled/outage runs from identical DB fixtures. Compare papers, candidates, evaluations, recommendations, profile IDs, feedback applications, workflow states and extraction results, excluding generated timestamps/IDs only where necessary. |

An in-memory exporter proves instrumentation structure only. A local HTTP receiver
proves serialization/transport behavior only. Neither proves Phoenix ingestion,
rendering, retention, or Mini resource behavior.

## Actual Phoenix and Mini evidence checklist

Keep these items marked NOT RUN until observed; the dashboard opening is not proof
that traces are correctly ingested or linked.

- Record installed Phoenix version/image digest and Project Paper revision.
- Verify effective loopback binding, persistent volume, SQLite location, retention
  setting, analytics setting, external UI resource setting, and OTLP endpoint.
  Inspect configuration without copying credentials or entire environment dumps.
- Agree resource limits and observation window before the run. Capture baseline
  and after-run Phoenix CPU, memory/current and peak, process/thread count, disk
  usage, and exporter overhead. Record other concurrent Mini jobs as confounders.
- With authorization, send a synthetic trace through the actual exporter and
  endpoint. Find its exact trace ID in Phoenix and verify span count, nesting,
  model/tool rendering, timings, numeric usage, unknown usage, and sanitized errors.
- For one separately authorized pipeline smoke run, correlate workflow and run IDs
  with actual DB rows and Phoenix trace. Do not infer success from HTTP 200 alone.
- Confirm persistence across an authorized Phoenix restart. Confirm retention by
  effective configuration initially; record deletion behavior separately when
  observed. Do not claim 14-day deletion has been exercised immediately.
- Exercise outages and saturation on disposable local infrastructure first. Any
  actual Mini stop/restart, resource stress, or production run needs its own authorization.

## Evidence record and release gate

Record exact tested hashes/revision, commands, test counts/skips, fixture vs actual
environment, timing/resource observations, and untested items. Report independent
QA pass/fail to developer, architect, and observability owner. Local correctness
clearance and actual Mini/Phoenix acceptance are separate verdicts. Hold commit/push
for the user's approval workflow; do not deploy from QA.

## Independent local QA result for initial patch

Baseline: `a80aea9fbbdce0878f740c55ce31335f10b5241c`, plus the reviewed
uncommitted telemetry implementation and hooks. Local correctness PASS; actual
Phoenix/Mini acceptance remains NOT RUN.

- Optional-SDK full suite: 317 total, 312 passed, 5 existing skips, 66.076 seconds.
- Subsequently added independent `tests/test_telemetry_qa.py`: six tests passed.
  These cover concurrent workflow/chunk parent isolation, span-start fault
  isolation, recovery after an exporter exception, six workflows behind a blocked
  exporter, successful recommendation/review DB parity off/on/export-failure,
  and a real local HTTP receiver that accepts but does not send response headers.
- Wait assertions allow scheduling overhead: complete synthetic calls under
  0.65 seconds; explicit shutdown under 0.5 seconds. The implementation's requested
  flush/join wait is 0.25 seconds. These checks do not establish a hard real-time
  bound under arbitrary host load.
- Local HTTP stub verified standard OTLP protobuf decoding and content type.
  Serialized payload privacy and resource checks passed. Diff whitespace check passed.
- Reviewed telemetry module SHA-256:
  `09d5d31e9e11f0fedf29082062a98669660335829cf47298f80b6a8adc8d7a4c`.
- Reviewed tracked application-hook diff SHA-256 (`git diff -- paper_agents`):
  `4607696c1b57b39358f5cbc4cdb69c138524fa2fe9a6b01bcd5b5594f8587999`.

Residual limitation accepted conditionally by architect for opt-in pilot:
indefinitely stuck transport can require process restart; one worker and bounded
queue remain, but traces can be incomplete. Missing spans do not establish missing
execution and cannot support availability/quality denominators. The one-second
socket timeout is not a total export deadline. Linux/Mini timing, actual Phoenix
rendering/configuration/retention, and resource measurements require separate
evidence. No live model, production pipeline, Phoenix, deployment, commit, or push
was performed by QA.
