# MiniLM inference extraction — Developer handoff

This source change starts from cached main `2ee45dcc3d84d98070c7267fe78983477559e9eb`,
not the AWS-derived bootstrap history. The existing scorer and Curator interest
module match that baseline. It does not supply the separate serving bootstrap/UI
ports discussed in b8f71d6. SDLC owns their integration on an approved app baseline.
No production default, ranking logic, Qwen prompt, cron or host is changed.

## Model and client contract

The service constructs the existing `MiniLMScorer` once before opening its HTTP
listener. It preserves ONNX CPUExecutionProvider, two intra-op threads, one inter-op
thread, sequential execution, tokenizer max_length512/truncation/padding settings,
and finite-score checks. The model SHA256 remains
`5d3e70fd0c9ff14b9b5169a51e957b7a9c74897afd0a35ce4bd318150c1d4d4a`, revision
`233902d25c440f23af6f7d6e94d2946bac0bee0a`. ONNX Runtime1.23.2 and Transformers4.57.1
use the existing requirements file. Transitive dependency versions must be captured
from the built image; no new lockfile or reproducibility claim is implied.

`PAPER_AGENT_MINILM_BACKEND=embedded` remains the default; absence of this variable
is unchanged production behavior. Set `http` explicitly plus
`PAPER_AGENT_MINILM_URL=http://paper-minilm:8091` and optional
`PAPER_AGENT_MINILM_TIMEOUT=10` to select the client. Unknown backends fail. No
implicit retry, proxy, redirect, invented score or embedded fallback occurs.
`PAPER_AGENT_MINILM_ENABLED` remains a separate pipeline enable switch.

The HTTP client replaces only `curator_interest.default_interest_scorer`'s callable.
Curator still normalizes title/valid abstract, deduplicates configured positive
interests, performs per-interest scoring, maximum selection and existing stable
ordering. Evaluation/shadow paths remain embedded. No score calibration changes.
On HTTP failure the existing caller records `interest_fit.status=unavailable`,
score=null, with a sanitized reason. Existing Curator continues other evaluation;
this is NOT a new fail-closed pipeline or a fabricated zero score.

## Private API v1

- GET `/ready`: HTTP200 only after model construction; includes ready=true,
  identity, API version, tokenizer-file SHA256s, installed runtime versions and
  configured request/concurrency limits. Readiness means loaded, not spare capacity.
- GET `/version`: same provenance without input/user data. No model-management API.
- POST `/v1/score`, Content-Type application/json, one Content-Length, no chunked
  encoding. Body: `{"api_version":1,"pairs":[{"title":"...","abstract":"...",
  "interest":"..."}]}`. One to32 pairs, maximum262144 encoded bytes.
- Successful response: `{"api_version":1,"identity":{...},"scores":[...]}` in request
  order. All scores finite; client verifies exact pinned identity and response
  count/size/types. API rejects invalid/missing abstract/title/interest with422.
  The normal app caller handles missing/invalid metadata before calling the API.
- 413 oversized body;400 invalid framing;408 socket read timeout;503 overload or
  inference error;504 exhausted batch budget. Error responses contain fixed codes,
  never request text. Access/error content logging is suppressed.

Four handler slots and backlog4 bound connections; inference concurrency is1 with
no inference queue. Excess work receives503; clients do not retry automatically.
Socket idle/read timeout3s; client timeout defaults10s (allowed0–30s); server batch
budget10s checked between and after scores. **An in-flight ONNX call is not forcibly
cancelled**: a client timeout does not free the inference slot until that call
returns. Later requests receive503; never spawn replacement inference workers.
The ONNX model is fixed and input bytes bounded, but measured worst-case execution
is still an acceptance gate. An engine hang requires operator service stop; no
automatic restart storm. Socket timeout is an inactivity timeout, not a hard
whole-request deadline. This is a trusted internal service, not an Internet API.

## Container and SDLC integration

Build `deploy/minilm/Dockerfile` on an approved CI/development builder only, with
`MINILM_BASE_IMAGE` set to an approved Python3.12 slim Debian immutable base digest,
platform linux/amd64, and immutable result digest recorded. No build ran here.
No service image digest is yet available; never substitute a fake parser digest.

`deploy/minilm/compose.yml` is a separate project `paper-minilm-inference`. It has
no host port, application DB/feedback/config/PDF volume, credential or provider
access. Only an existing verified model-only directory is bound read-only at
`/models/minilm`, create_host_path:false. Root filesystem read-only, UID10001,
capabilities dropped, no-new-privileges,16MiB tmpfs. No downloads, writable model
cache, model administration endpoint or automatic image pull/restart.
Model hash is verified by the existing scorer; tokenizer loads local-only with
trust_remote_code:false. Tokenizer hashes are exposed for baseline comparison;
they are not newly hard-coded approved hashes. Validate the complete model/tokenizer
manifest against the approved embedded deployment before any parity claim.

Provisional service cap:512MiB memory, memory+swap512MiB (no swap),1 CPU,64 PIDs,
two1MiB log files. Preserve scorer thread settings even under CPU quota. These
limits need measured startup/steady/long-input memory and latency; no actual-model
resource qualification has occurred. Budget service, app, Qwen, dockerd and existing
production work together. No swap/host resizing, downloads or new cloud resources.

SDLC must create/verify an isolated **internal** Docker network `paper-mini-inference`
(external to these Compose projects) after scoped host approval. Existing same-name
networks require identity/driver/internal/attachment inspection, not blind reuse.
Attach only the service and authorized nonprod client initially. Docker membership
is the trust boundary; there is no bearer secret or TLS inside this private network.
No host/public publish, host-gateway, AWS connection or production attachment.
`nonprod-client.override.yml` supplies the client network and opt-in variables;
render/inspect the merged Compose with the chosen nonprod baseline. It does not
turn off serving-only mode or enable MiniLM pipeline jobs. Bootstrap therefore
continues its safe initial mode; use explicit synthetic scoring tests first.

Qwen/Ollama configuration is untouched. Shared Qwen access and cross-production
inference admission remain SDLC/Architect gates. Do not attach production to the
new network or set its HTTP backend during nonprod validation. Later separately
approved production switch changes only backend/network wiring after parity,
resources and failure behavior are accepted. Roll back to embedded by restoring
that backend and its prior qualified read-only model access; preserve production
state and prior image. Stop only this inference project's service on failure;
never stop/prune production. No scheduler/cron edits.

## Validation and remaining gates

Local HTTP tests use real loopback transport and a deterministic fake scorer to
cover load-once, multiple interests/max, stable ties, invalid metadata, size/batch
limits, overload, service unavailable/timeout, nonfinite result, failed model
load, response validation and opt-in/default behavior. Existing Curator and MiniLM
evaluation tests remain relevant and must pass. These are not ONNX parity tests.

`scripts/minilm_api_parity.py --url <approved-internal-origin> --model-dir
<approved-local-model-dir>` runs the actual embedded model against the actual API
on fixed synthetic multiple-interest, long/truncated, missing/invalid abstract
and missing-title cases. It checks every per-interest score, max/winner and stable
order. Initial numerical gate is abs/rel1e-6: the same CPU graph should be repeatable;
record observed max delta on the selected engines before accepting that tolerance.
Do not widen it to pass or claim it empirically established before execution.
No input text is printed. Capture runtime/model/tokenizer manifests alongside it.

The optional real-model unittest runs this harness against a loaded local service
when ONNX/Transformers and the pinned model exist; otherwise it explicitly skips.
This workstation lacks ONNX Runtime, Transformers, NumPy and the known model/cache
paths. No packages or models were downloaded. Real numerical parity, built-image
identity, model-filesystem permissions, container limits/network isolation and
Mini resource/production-health qualification remain OPEN. No SSH, host mutation,
main merge, workflow dispatch or production switch is part of this work.

Local validation result:7 API/transport tests passed with ResourceWarning treated
as an error;1 real-engine test skipped for missing offline runtime/model.40 existing
Curator v2/v3/interest and MiniLM evaluation tests passed (existing SQLite resource
warnings suppressed in the final broader run). Compose service config parsing,
client override YAML and Python compilation passed. No container/image build or
real inference measurement was performed.
