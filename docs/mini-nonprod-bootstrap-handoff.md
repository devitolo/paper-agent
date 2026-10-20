# Consolidated Mini non-production preparation — SDLC

Status: locally integrated and exact AMD64 images checked under ARM emulation.
NOT real-model-qualified or Mini-host-qualified. See verified identities below. No Mini access,
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
- Three fake-client timing-harness tests pass after import (86 total focused
  source tests). Four additional read-only model-inventory helper tests pass.
  Seven API tests also pass inside the exact AMD64 service image,
  with the real-engine test still skipped.
- Both images were built locally from the hashed source archive. Offline app-image
  smoke passed check-state, pages/badge/blocked routes and restart persistence at
  the intended512MiB/1CPU limits. Runtime imports, pip check, installed-package
  inventories and source-byte comparison passed. Temporary local objects removed.
- These checks used AMD64 emulation on an ARM workstation. No real-model parity,
  native latency, model memory/load capacity, or Mini production-impact claim.

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

## Verified images and remaining read-only gates

Application code/source revision remains
`d6522ee78eb37675e01d8164c8b1f4674e83d3ff`; subsequent documentation/operator-tool
commits do not change the already built image bytes. Source archive SHA256:
`7eecf7405583f82ac6ca0245c51f960daa21788d56172b245dfc313b1ca7ce84`.
Base is official Python3.12.15 slim Bookworm, linux/amd64:
`docker.io/library/python@sha256:2ed6491b93cd49272ee6de2b5a38440c3448360322c089fc23e370722d74179d`.

App immutable local reference:
`paper-mini-nonprod-prep@sha256:a8b50c80b157711be04e8bf52a4ac4db0e4cde94909f53a267285f8a3cd8cf81`.
Config digest:
`sha256:7947da6941df0e0995072807ecace15179afc0bcc74da7355ada4555be04b7fb`.
`app-amd64.docker.tar`:52,509,696 bytes, SHA256
`ab854fac57b0d49f8cb9a0c41026e4704653c1a1d24c41880294b347b661e932`.

MiniLM immutable local reference:
`paper-minilm-prep@sha256:81d1d7b5ff54fd02fffa934c05c36927c83cee38ccdb980dcebd011aadfe2739`.
Config digest:
`sha256:0a5a5af1f2bbbb3a0ea9c3a08f9d5a8d3f5ca5b35915ebd9cc51e747699fabce`.
`minilm-amd64.docker.tar`:158,511,616 bytes, SHA256
`2bbebeba8de9c3109191ee60af69fa545380c2c6c3e4c4bc37dad30f854dd3d8`.

Both archives include OCI manifests/configs/layers with verified blob hashes.
Combined archive size is211,021,312 bytes; expanded layers total653,227,520 bytes
when shared base layers are conservatively counted twice. Reserve those costs,
synthetic state/log/helper overhead and existing backups before applying the
2GiB remaining-free-disk gate.
They are LOCAL exports, not published registry images. Transfer only these exact
archives after approval and verify SHA256 before `docker image load --input`.
Use `pull_policy: never`. Some Docker stores report config ID rather than manifest
ID, or do not restore RepoDigests from an archive. After checking exact manifest,
config, platform and labels, use the expected manifest reference if available;
otherwise only the exact recorded config SHA256 is permitted as a local immutable
reference. Never substitute a mutable tag or pull this local name from a registry.
If neither approved identity resolves locally, STOP; no format conversion/rebuild.

Evidence directory: `/private/tmp/paper-mini-nonprod-images-d6522ee`.
Machine-readable identities: `deploy/mini-nonprod/prepared-images.json` and the
external `qualification-manifest.json`. `app-runtime.json` and
`minilm-runtime.json` contain installed packages and SHA256s of every app Python
source file; source bytes match the reviewed archive. Service includes ONNX1.23.2,
Transformers4.57.1, NumPy2.5.4 and tokenizers0.22.2; remaining resolved dependencies
are recorded, not claimed reproducibly locked. Imports and `pip check` pass;
missing model is rejected before binding. There was no model download.

Still unknown until the approved read-only gate:

1. Actual production model-only source and complete tokenizer/config hashes.
   Recorded expected model is cross-encoder/ms-marco-MiniLM-L6-v2, revision
   `233902d25c440f23af6f7d6e94d2946bac0bee0a`, ONNX SHA256
   `5d3e70fd0c9ff14b9b5169a51e957b7a9c74897afd0a35ce4bd318150c1d4d4a`.
   Constants originate in `557d283` (2026-09-26); runtime pins in `401c1e2`
   (2026-10-06). Historical scripts mount `paper-agent-minilm-model-cache`
   read-only at `/models`, with expected model subdirectory `/models/minilm`.
   These are historical wiring, NOT verified current Mini paths or contents.
2. Verify the production mount inventory without reading DB/config/secrets. Resolve
   ONLY its model subtree, inspect ownership and absence of aliases, and hash the
   complete tree with `scripts/mini_nonprod_model_manifest.py` via SSH stdin
   (no saved remote script required). This helper reads only, caps inventory at
   120s/1GiB/1000files, rejects aliases/changed files and wrong ONNX hash. Missing
   tokenizer assets, unidentified source, extra non-model contents, unreadable
   permissions or any mismatch means STOP before creating services. Record the
   observed tokenizer manifest as the current-production-file baseline, NOT as
   independently attested upstream provenance. Use those same frozen files read-only
   for both parity instances; compare the entire manifest before/after tests.
3. Mini home/new install path, CPU/Docker/Compose/cgroup capabilities, identities,
   capacity and job-free window still need fresh verification. No previous SSH
   permission carries forward. Check current production runtime versions as
   metadata only; any discrepancy must be reported before claiming compatibility.
4. Shared Qwen origin and admission remain unqualified. This window may inspect
   existing endpoint/network metadata read-only, but does NOT connect nonprod to
   production Ollama, call Qwen, or attach/reconfigure any production network.
   Qwen stays disabled. Shared PDFs and full pipeline execution remain deferred.

`deploy/mini-nonprod/Dockerfile` builds the synthetic serving UI only; its explicit
base ARG intentionally has no mutable default. The service uses Developer's
Dockerfile plus recorded source/base labels supplied at build. No Mini build,
package/model download, new cloud resource, registry publishing or production CI
is needed. Real numerical parity and native capacity are tests in the proposed
window, not completed preparation results.

## Proposed single future Mini validation/setup window (NOT approved)

Target `devitolo@192.168.1.110`. Proposed NEW root
`/home/devitolo/workspace/paper-mini-nonprod` must first be verified against actual
home/path ownership; never modify `~/workspace/paper-agent` or production files.
Scope must name final images, manifests, model path, paths/names, resource limits,
operator, time budget, and permission for any artifact load/transfer. The unresolved host/model inputs above are conditional read-only STOP gates.
Approval permits proceeding to creation only if every preflight gate passes.

1. Read-only baseline: host identity/architecture, Docker/Compose/cgroup/network,
   production image/mounts/health/restarts/OOM/latency, active jobs and schedules,
   memory/pressure/swap/CPU/disk/inodes, port and all planned identities. Compare
   production cron byte-for-byte before/after without printing sensitive commands.
   Architect's previous ~6.6GiB available/793GiB free is historical evidence only.
   AWS synchronization is unconfirmed and is never exercised during this window.
   For production latency use the verified read-only `/ready` handler only, with
   20 one-second samples before, during and after load. Do not probe production
   pages that may initialize/migrate data. Other HTTP page smoke tests target only
   nonprod. Do not print secret-bearing cron/environment values. User/operator
   must avoid manual production pipeline/model work during this window; verify no
   scheduled run falls within it and monitor active jobs throughout.
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
   Use only the exact new projects; record object IDs/labels and mounts.
5. Start only the new MiniLM service on the internal network; observe readiness
   for at most60s, verify `/version` model/tokenizer/runtime manifest, effective
   read-only mounts, limits, no host port, and network membership. Pause on mismatch.
6. Real parity runs from a one-shot approved service-image client with read-only
   model and validation-script mounts, PYTHONPATH=/app and overridden Python
   entrypoint. The service image does NOT contain the scripts by default; mount
   only the reviewed `scripts/minilm_api_parity.py` and measurement helper from the
   source bundle, not a repository/config/production directory. Run:
   `python /validation/minilm_api_parity.py --url http://paper-minilm:8091 --model-dir /models/minilm`.
   Enforce an outer120s wall-clock bound; capture the exact new client container ID
   and stop that client on timeout (killing only a Docker CLI is insufficient). Require every per-interest/max/winner/order
   comparison and abs/rel1e-6; never widen tolerance or ignore unavailable scores.
7. Stop parity client; observe30s idle. Run API-only measurement helper (no second
   model) with outer90s bound, at most7 fixed calls, no retries. Capture verified
   service cgroup peak/current memory, CPU deltas, first/short/long/batch latency
   and production health. These are smoke samples, not p95/p99/load qualification.
   ONNX is not hard-cancelled by client timeout; stop only new service on hang.
   See Developer's `docs/minilm-validation-window.md` for exact counter commands.
8. Inference portion maximum5minutes; total proposed operator window30minutes.
   Monitor at10s intervals. Immediate STOP on OOM/restart/readiness failure, new
   production job, changed cron/image/mount identity, MemAvailable<512MiB, swap-used
   increase, swap I/O across two consecutive samples, memory PSI some avg10>=5%
   or full avg10>=1%, or a sustained rising pressure/production-memory trend.
   Proposed production latency gate: observed p95 must stay at or below
   max(2 x baseline p95, baseline p95 +500ms), with no errors. This is a bounded
   smoke stop rule, not statistically qualified SLO evidence. No stable baseline
   means inconclusive/STOP. Nonprod UI/API calls must finish within10s; readiness
   within60s; no unexpected5xx. The single deliberate overload probe expects503
   and is reported separately. Do not loosen any gate or rerun automatically.
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

## Approval wording for this exact preparation

Approve ONE30-minute SSH validation/setup window on devitolo@192.168.1.110,
starting with the read-only gates above. If and only if those gates pass, permit
transfer and offline loading of the two exact image archives, reviewed bootstrap
files/operator helper and hashed synthetic fixture; creation of the five NEW
nonprod volumes, two NEW internal networks, nonprod app and MiniLM service plus
bounded temporary clients; and the specified smoke/parity/resource checks. No
production container/network/config/DB/model/cron writes, no AWS action, no remote
download/build, no Qwen inference, no shared PDFs, no promotion or production
backend switch. Stop new workloads at timeout/failure/completion and retain their
state/evidence; remove only named temporary validation/helper containers. Continued
operation and data/volume deletion are excluded. If path/model/tokenizer provenance,
image identity, capacity or schedule cannot be established, report the blocker and
stop rather than expand the scope. Approval is task-scoped, not ongoing SSH access.
