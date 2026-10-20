# Pending single Mini validation window — SDLC owns approval and execution

No Mini SSH/access/change is authorized by this document. Developer checked local
offline inventory only: Darwin ARM64 Python lacks ONNX/Transformers/NumPy; bundled
Python3.12.14 ARM64 has NumPy but no ONNX/Transformers. Known model/cache paths and
archived experiment directory are absent; scoped repo/tmp search found no model
or tokenizer assets. Local Docker is Linux aarch64,10 CPUs,8319504384 bytes RAM,
with cloud/Gemini/unrelated images and no qualified MiniLM service image. No
packages/models/images were installed, transferred or downloaded. No real-model
numerical, latency, CPU or memory qualification has run.

SDLC consolidates this with bootstrap setup into one user request. Specify exact
host, image digests/source, read-only model path and manifest, isolated names,
network attachments, total time/resource bounds and stop owner before approval.
No production default/config/cron/service/volume changes, no AWS action, no model
administration and no pruning. Any image transfer or network/container creation
must be explicitly within that later approved window. Stop on ambiguity.

## Sequence and measurements

1. Before mutation, record production readiness/latency and schedules, host memory,
   pressure/swap, disk/inodes, Docker/cgroup support and existing nonprod names.
   Budget two model processes for numerical parity (embedded client plus service),
   but only one for service-only measurements. Account for Qwen and production
   jobs; do not overlap shared inference with scheduled production load.
2. After the window is approved and reviewed service start is permitted, record
   monotonic start time immediately before starting ONLY the isolated service.
   Poll its existing internal `/ready` at1s intervals for at most60s from the
   approved client network; stop on timeout/model identity failure. This interval
   measures container startup+model load+readiness, not just the first inference.
   No health probe is permission to launch/restart a container automatically.
3. Verify `/version` model/query/runtime/tokenizer manifests, read-only model mount,
   no host port/production attachments,512MiB/no-swap1CPU64PID effective limits and
   internal network. Determine the service's exact full container ID from the
   explicit approved Compose project. Example read-only identity selection:

```bash
docker --host unix:///var/run/docker.sock ps -a --filter label=com.docker.compose.project=paper-minilm-inference --filter label=com.docker.compose.service=minilm --format '{{.ID}} {{.Names}} {{.Status}}'
```

Require exactly one matching reviewed service; do not operate on an ambiguous
selection. Set `MINILM_TEST_CONTAINER_ID` to its verified full ID. Capture baseline,
idle, inference and final measurements using these read-only commands:

```bash
docker --host unix:///var/run/docker.sock inspect --format 'image={{.Image}} memory={{.HostConfig.Memory}} swap={{.HostConfig.MemorySwap}} cpus={{.HostConfig.NanoCpus}} pids={{.HostConfig.PidsLimit}} oom={{.State.OOMKilled}} restarts={{.RestartCount}}' "$MINILM_TEST_CONTAINER_ID"
docker --host unix:///var/run/docker.sock stats --no-stream --format '{{.ID}} cpu={{.CPUPerc}} memory={{.MemUsage}} pids={{.PIDs}}' "$MINILM_TEST_CONTAINER_ID"
docker --host unix:///var/run/docker.sock exec "$MINILM_TEST_CONTAINER_ID" python -c 'from pathlib import Path; names=("memory.current","memory.peak","memory.events","cpu.stat"); print({n:(Path("/sys/fs/cgroup")/n).read_text() if (Path("/sys/fs/cgroup")/n).is_file() else "unavailable" for n in names})'
```

The last command assumes cgroup-v2 exposes the container scope; verify namespace
scope first. Unavailable counters are not zero. On v1, SRE must choose equivalent
verified container counters before measuring. Do not alter/reset counters or
limits to get a passing result. `memory.peak` may include startup and prior calls;
report that scope. CPU usage_usec delta divided by wall time gives CPU demand;
Docker samples alone can miss short peaks. `docker exec` itself has small overhead.

4. Run actual numerical parity only in the approved client container/environment:

```bash
python3 scripts/minilm_api_parity.py --url http://paper-minilm:8091 --model-dir /models/minilm
```

Both paths need the exact approved model/tokenizer/runtime. Preserve abs/rel1e-6;
record maximum observed delta and exact order/winner agreement. Missing dependencies,
asset mismatch or unavailable scores means failure, not permission to download or
relax tolerance. This process loads a second embedded model; do not use its RSS as
service-only memory. The parity script does not capture host resource counters.

5. Stop the parity client process, keep the reviewed service, observe30s idle,
then run the API-only timing harness from the approved client environment:

```bash
python3 scripts/minilm_service_measure.py --url http://paper-minilm:8091
```

It loads no embedded model, sends at most seven sequential synthetic requests:
first call, two short, two long (>512 tokens), two32-pair batches. No retry. It
stops admitting calls after60s, with the in-progress call subject to10s socket
inactivity timeout. This is not a hard70s deadline against a slow streaming peer;
the operator window must supply an outer process limit if required. An incomplete
report exits nonzero and gives no private request text. First-request latency is
not model startup time. Small sample counts are smoke measurements, not p95/p99
claims or production throughput qualification. Capture host/cgroup counters in
parallel at bounded intervals, and production health before/during/after.
6. Reproduce overload with one active request plus one extra: expect503, no retry
storm or extra model load. Run the local fake-engine tests for deterministic
unavailable/timeout paths rather than disrupting production/network. An in-flight
ONNX call cannot be forcibly cancelled by client timeout; retain occupied slot,
stop only test service on a hang and record failure. Never create parallel workers
as a workaround. Any OOM, unexpected restart, memory pressure or production
regression ends the test; do not increase quotas or lower acceptance thresholds.
7. End stopped unless the user separately permits continued nonprod operation.
Preserve evidence and exact images/model state; no production switch or model/data
pruning. SDLC reports completed/unrun checks in one consolidated handoff.

The measurement harness has only been tested with a fake client locally. No
numbers from it have been collected against a real MiniLM engine here.
