# Consolidated Mini non-production preparation — SDLC

Status: locally integrated, NOT image-qualified or host-qualified. No Mini access,
installation, container/image/network/model/config/cron change, AWS operation,
production switch or branch promotion is authorized by this document.

This replaces the original bootstrap handoff's AWS-derived schema/runtime
assumptions. SDLC owns this single setup plan; Developer owns the MiniLM API and
client; Architect owns shared-inference boundaries; SRE owns host/resource and
production-stability acceptance. Existing production cron remains unchanged.

## Exact source and implementation boundary

Base: cached main `2ee45dcc3d84d98070c7267fe78983477559e9eb`.
Independent MiniLM implementation: `35636181f3823e317d25c32555c87ce51e494b71`.
Only original bootstrap delta: `b8f71d6fe7ff69d332d357c3a8f12cbad591b1b8`,
cherry-picked locally as `cae18e4` (five files). No AWS parent history was merged.
Developer timing preparation: `28831b6c36dc40e876e7369594273f601d7d2ab8`.
Use the final integration HEAD and SHA256 source-bundle manifest as delivery
identity; original commits alone do not contain the completed integration.

Integration adds a synthetic-only read-only state validator, explicit nonprod
startup guards, existing lifecycle lease, disabled Scout/Topic Agent/profile
inference, and the escaped NON-PROD badge/watermark. Default fresh/imported and
embedded MiniLM behavior stays unchanged. Every serving UI database initialization
validates the exact checked-in schema and synthetic marker, without seeding or
migration. Unknown/missing/aliased state fails. There are no sync tables or routes
in this integration: `/sync/input` and `/sync/results` return404. An empty sync
secret is still required; absent routes are stronger evidence than claimed auth
behavior for code that is not present. Do not add AWS exchange modules to fix this.

Fixture generation creates one synthetic paper/feedback, empty profile and topics,
explicitly disabled valid industry config, and separate logs/control/generated
state. It refuses an existing destination. The former `{}` industry config was
invalid for the Topics page and is fixed. No production data/config is copied.

## Local validation and precise limits

- Nine bootstrap/integration tests pass: actual HTTP `/`, `/topics?mode=agent`,
  `/health`, `/runtime`; badge; feedback persistence after reinitialization;
  blocked Scout/Topic Agent/sync routes; exact-schema/no-reseed checks; lifecycle
  entry; unsafe-env rejection; actual Compose merge preserving isolated storage.
- 67 selected package-runtime, Curator and UI regression tests pass. Existing
  SQLite ResourceWarnings were suppressed on that run; no full-suite claim.
- Seven MiniLM HTTP/API fake-engine tests pass with ResourceWarning as errors.
  One actual-engine test skips because the offline model/runtime is unavailable.
- Developer supplied three fake-client timing-harness tests; rerun after import.
- No container was built or started; no measured image memory, model parity,
  inference throughput, or Mini load qualification is claimed.

Commands from the isolated integration checkout:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests/mini_nonprod -v
PYTHONDONTWRITEBYTECODE=1 python3 -W error::ResourceWarning -m unittest discover -s tests/minilm_service -v
PYTHONDONTWRITEBYTECODE=1 python3 -W ignore::ResourceWarning -m unittest tests.test_package_runtime.PackageRuntimeTests tests.test_curator_interest tests.test_curator_v2 tests.test_curator_v3 tests.test_ui_qa -q
```

## Isolated objects and resource envelope

App project `paper-mini-nonprod`, service `app`, loopback127.0.0.1:18080 to8000.
Five external volumes: `paper-mini-nonprod-data`, `paper-mini-nonprod-config`,
`paper-mini-nonprod-logs`, `paper-mini-nonprod-runtime-control`, and
`paper-mini-nonprod-generated`. All must be newly created, labelled, owned10001:10001;
existing same-name state is a STOP, not a reason to reuse or overwrite.
Internal app network `paper-mini-nonprod-internal`.

MiniLM project `paper-minilm-inference`, service `minilm`, alias `paper-minilm:8091`.
External-to-Compose internal network `paper-mini-inference`, initially only
MiniLM, nonprod app, and temporary approved validation clients. No production
attachment or public port. Only model-only directory mounted read-only with
create_host_path:false. No application DB/config/PDF/credentials in the service.
No shared PDF mount at all in this phase.

Render the app with ONLY bootstrap Compose and, for MiniLM stage, the dedicated
client override. Compose merges `isolated` and `inference`; tests confirm both.
The override sets HTTP backend/URL but intentionally retains MiniLM pipeline=0,
serving-only=1 and scheduler=0. Exercise synthetic API calls explicitly; this is
not a full discovery/curation environment or automatic pipeline enablement.

App and service each:512MiB/no swap/1CPU; app128PIDs/service64PIDs;16MiB tmpfs;
read-only root, dropped capabilities, UID10001, no automatic pull/restart.
Parity client also needs a separately bounded512MiB/1CPU allocation because it
loads a SECOND model. Minimum prestart review inputs:1GiB available for app-only;
2GiB before the combined app/service/parity stage, plus measured image-load and
Docker overhead. Retain at least512MiB host headroom throughout. These are stop
floors, not proof that512MiB is sufficient for a model. No quota increases to pass.
Any OOM/restart, production health regression, sustained new memory pressure/swap
churn or scheduled production work overlap stops test workloads. SRE must qualify
actual latency limits before execution. Preserve production reserves and backups;
require at least2GiB disk free after measured artifact costs, inodes below80%.

## Unresolved delivery inputs — do not substitute guesses

1. Approved pinned Python3.12 slim Debian base and resulting linux/amd64 app and
   MiniLM image digests, source provenance and runtime inventory. No valid digests
   exist from this work. Parser placeholder digests are never deployment inputs.
2. Approved offline model/tokenizer manifest and verified absolute model-only
   directory. ONNX expected SHA256:
   `5d3e70fd0c9ff14b9b5169a51e957b7a9c74897afd0a35ce4bd318150c1d4d4a`;
   revision `233902d25c440f23af6f7d6e94d2946bac0bee0a`.
   Tokenizer hashes still need comparison with the approved embedded deployment.
3. Local host is ARM64. Homebrew Python lacks onnxruntime/transformers/numpy;
   bundled Python3.12 has numpy but lacks ONNX/Transformers. Known `/models/minilm`
   and Hugging Face cache paths are absent. No qualified service image is present.
   No packages/assets will be downloaded or copied from Mini without authorization.
4. Mini absolute home/install/model paths, effective runtime/cgroup support,
   free port/names, production identities and schedules need fresh scoped access.
5. Shared Qwen needs a verified existing inference origin and enforced admission
   policy. Do not attach/reconfigure the production Ollama container, expose its
   management API, or assume our nonprod lock coordinates with production cron.
   Qwen stays disabled until that bounded route and non-overlap policy are concrete.

`deploy/mini-nonprod/Dockerfile` is a dedicated synthetic serving image: no Gemini,
Node, Poppler, model preparation or production image target. The MiniLM service
uses Developer's Dockerfile. Build only on an authorized builder, record base and
result digests/runtime dependencies, and run image-specific check-state/readiness.
No build on Mini and no reuse of unqualified cloud images.

## Proposed single future Mini validation/setup window (NOT approved)

Target `devitolo@192.168.1.110`. Proposed NEW root
`/home/devitolo/workspace/paper-mini-nonprod` must first be verified against actual
home/path ownership; never modify `~/workspace/paper-agent` or production files.
Scope must name final images, manifests, model path, paths/names, resource limits,
operator, time budget, and permission for any artifact load/transfer. All unresolved
inputs above must be completed or explicitly made conditional STOP gates.

1. Read-only baseline: host identity/architecture, Docker/Compose/cgroup/network,
   production image/mounts/health/restarts/OOM/latency, active jobs and schedules,
   memory/pressure/swap/CPU/disk/inodes, port and all planned identities. Compare
   production cron byte-for-byte before/after without printing sensitive commands.
   Architect's previous ~6.6GiB available/793GiB free is historical evidence only.
   AWS synchronization is unconfirmed and is never exercised during this window.
2. Stop on conflict/unhealthy production/active production job/insufficient capacity.
   Verify hashes and approved immutable AMD64 images already present or load only
   specifically approved archives after disk checks. Never pull/build/prune silently.
3. Create only the new directory, five labelled volumes and permitted internal
   networks. Seed from the synthetic fixture only, using an explicitly scoped
   offline helper; inspect volume sources/ownership/hashes. No production mounts.
   Verify network internal=true and exact members before connecting any client.
4. Render explicit project/file/env Compose and inspect image/mounts/ports/limits.
   Run app-image `python -m paper_agents.package_runtime check-state` offline with
   the same five mounts and serving env. Then `up --no-build --pull never -d app`.
   Check health, runtime, visible badge and synthetic feedback; restart only app
   and verify persistence. Confirm unavailable sync/worker/provider routes. Observe
   production health before/during/after. UI access only via an approved SSH forward.
5. Start only the new MiniLM service on the internal network; observe readiness
   for at most60s, verify `/version` model/tokenizer/runtime manifest, effective
   read-only mounts, limits, no host port, and network membership. Pause on mismatch.
6. Real parity runs from a one-shot approved service-image client with read-only
   model and validation-script mounts, PYTHONPATH=/app and overridden Python
   entrypoint. The service image does NOT contain the scripts by default; mount
   only the reviewed `scripts/minilm_api_parity.py` and measurement helper from the
   source bundle, not a repository/config/production directory. Run:
   `python /validation/minilm_api_parity.py --url http://paper-minilm:8091 --model-dir /models/minilm`.
   Enforce an outer120s process bound. Require every per-interest/max/winner/order
   comparison and abs/rel1e-6; never widen tolerance or ignore unavailable scores.
7. Stop parity client; observe30s idle. Run API-only measurement helper (no second
   model) with outer90s bound, at most7 fixed calls, no retries. Capture verified
   service cgroup peak/current memory, CPU deltas, first/short/long/batch latency
   and production health. These are smoke samples, not p95/p99/load qualification.
   ONNX is not hard-cancelled by client timeout; stop only new service on hang.
   See Developer's `docs/minilm-validation-window.md` for exact counter commands.
8. Inference portion maximum5minutes; total proposed operator window30minutes.
   Stop app/service/clients at end and retain evidence/volumes. Use exact projects
   and explicit files/env for `stop`; never global stop/prune, volume deletion,
   production network disconnect, model admin, cron changes or AWS access.
   Recheck production health/images/mounts/restarts/OOM and unchanged cron.

Continued nonprod operation and any production backend switch need explicit user
approval. A production switch would retain the embedded backend/model mount for
rollback and must follow successful parity/resources/failure validation. It is
excluded from this setup window. Shared PDF reuse later requires a verified
PDF-only read-only subtree and demonstrated write/delete failure, never a DB/config
root. New schedules remain disabled.
