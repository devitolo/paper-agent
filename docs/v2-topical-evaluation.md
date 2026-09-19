# V2 topical relevance: first implementation gate

This standalone package implements the approved synthetic engineering gate in
`v2-model-evaluation`, starting at stable v1 commit `6de3685`. It does not import
production scoring, read production databases, access the Mini, call providers,
download dependencies, or modify production. The separately approved integration smoke enables only pinned offline ONNX MiniLM on three synthetic papers. Main remains stable v1.

## Approved design

The exact interests are captured in
`experiments/topical_relevance/config/approved-design.json` and checked by the
configuration contract:

1. enterprise AI platform/architecture/governance/organizational tradeoffs
2. AIOps/observability/incident response/diagnosis/RCA/logs-metrics-traces/system relationships
3. agent reliability/tool-agent coordination/context/failure handling/recovery/human oversight

Each interest is compared independently with the paper. Max of all three scores
is the paper score: any one topic may establish relevance. No usefulness, novelty,
rigor, evidence quality, negative preferences, recency, profile or history signal
enters scoring. The production evaluator is not reused.

All adapters receive exactly title, abstract and one interest. Candidate IDs,
source/query/provenance, labels, reviews and historical scores are excluded from
worker payloads. The keyword baseline counts distinct configured literal phrase
hits per interest, then takes max. Its phrase mapping is explicit input to the
freeze, not mined from profiles or feedback. Real evaluation keyword mappings are
still pending approval. The fake adapter is an engineering fixture, not a model.

## Input and text policy

The only enabled preparation input is an explicitly synthetic JSON fixture list
(maximum 30 records). Every record must have `synthetic: true`, a unique string
`id`, a title, abstract, and `abstract_kind: synthetic_source_abstract`. Source,
query, capture_date and record_key may be retained in the private manifest for
provenance checks; none is sent to adapters. No DB importer or source fetcher is
implemented at this gate. Data marked real and learned adapter kinds are rejected
before execution.

`title_chars`, `abstract_chars`, and `min_abstract_chars` are explicit text-policy
parameters, not implicit model defaults. Collapse whitespace, select a leading
word-bounded span independently for title and abstract, and record normalized
source offsets, original lengths, selected-text hash and truncation. This single
selection is made before choosing an adapter. Deduplicate complete normalized
title/abstract pairs before truncation, not their potentially identical prefixes.
Missing/nontext/too-short/unverified abstracts yield `insufficient_metadata` and
no worker start or inference. The checks reject obvious unusable text and summary
provenance, but cannot certify that arbitrary supplied text is a genuine abstract.
No semantic relevance filter selects the excerpt.

Token fit is explicitly unverified. A future approved tokenizer preflight must
select one common span that fits every model and interest/prompt overhead,
without backend-specific hidden truncation. Real tokenizer/model revisions,
quantization, thread counts, dtype and decoding settings are not guessed here.

## Modules and execution

- `contracts`, `dataset`, `text_policy`, `freeze`: input allowlists, provenance,
  common selection and immutable configuration/input/code/runtime fingerprints.
- `adapters`, `worker`: topic-only keyword or fake adapter, isolated in a child
  process with serial three-interest evaluation. MiniLM, SmolLM2 and BGE are
  recognized as disabled choices; there are no third-party runtime imports.
- `supervisor`, `runner`: wall/input/output/resource supervision, group cleanup,
  immutable per-paper checkpoints, resume and score/tie ordering.
- `evaluate`: blinded sheet and post-inference metrics. The worker never imports
  this label-handling module.

Each run loads one worker and measures load wall time separately. The first pair
is `first_after_load`; later comparisons are warm. Cold means a fresh worker,
not an empty OS cache. Reports include per-pair CPU/wall, per-paper elapsed,
nearest-rank p50/p95 with sample counts, adapter-call counts, and sampled cumulative
process-group peak RSS. Linux available-memory and swap counters are captured before/after; other hosts report null rather than invented values. No learned-model calls are made at this gate.

Resource limits are supplied explicitly in config, bounded by implementation
ceilings: 120s load, 60s per pair, 6000s total, 4 GiB process-group RSS and 256 KiB
combined output per response. Tests use much smaller synthetic limits. These are
engineering ceilings, not approved hardware budgets or performance predictions.
The wall deadline covers nonblocking input writes, stdout/stderr reads and each
sample. Local `ps` sampling is itself limited to 0.5s; watchdog detection can lag
by that sampling time. RSS samples target 100ms intervals, include the process
group and may double-count shared pages or miss short spikes. This is a sampled
watchdog, not a kernel-enforced exact physical-memory cap. Missing measurement is
recorded and can be configured to fail closed; actual hardware smoke must require
measurement. A detached child escaping its process group is outside this
supervisor's scope; enabled adapters do not daemonize.

Failure, cancellation, malformed output, excess output or memory stops the batch.
TERM is followed by group KILL and a bounded reap. No retries or fallback. Results
record fixed error codes and byte/resource measurements, never arbitrary stderr.
Cleanup failures invalidate the run. Labels are not used to decide retries.

## Artifacts and resume

All generated files must live outside Git worktrees. Output validation resolves
symlink aliases and rejects any ancestor with a `.git` file or directory. Output
parents must already exist, and output directories must be new. Writes use
fsynced temporary files and atomic no-clobber hard links. The filesystem therefore
must support same-directory hard links. Input aliases cannot be overwritten.

A single-writer lock protects a run. Per-paper intent is recorded before work.
A published outcome is immutable, hash checked and skipped on resume. Resume
requires exact manifest, config, code and Python/platform fingerprints. An intent
without a result is an unresolved in-flight attempt and is not silently retried;
start a separately approved new run after diagnosis. Failed outcomes and cleanup
failures cannot be resumed into success. Crash-created stale locks require manual
inspection; no automatic stale-lock deletion is implemented. Invocation summaries
are appended under new names. Completed results and their resource time remain
charged to the overall budget on resume. Lost in-flight work blocks resume.

## Evaluation semantics (synthetic protocol, real gate pending)

The blind sheet contains opaque keys, selected title/abstract and truncation flags.
It omits source/query, model results and prior labels. It may be prepared before
inference, but evaluation refuses to join labels until every candidate has a
published outcome. Blind labeling acknowledgment is an attestation, not proof
that the user has never seen a score. Existing reviewed/profile-influenced data
is development-only; this implementation does not manufacture held-out status.

For each adapter separately, exact score ties share rank. A frozen label-independent
hash seed breaks display/top-k ties. Cross-adapter comparisons require identical
candidate manifests, interests, aggregation, tie seed and metrics. Raw score scales
are not merged or compared across families.

Synthetic metrics are frozen as precision and graded relevance at 3/5, where
relevant=1, partial=0.5, unrelated=0; only relevant counts for precision. End-to-end
precision uses denominator k, so unfilled positions earn no credit. Also report
conditional precision over returned items with assessable labels, explicit
insufficient-metadata/failed/pending counts, missed-relevant and promoted-unrelated
counts, and boundary-tie precision ranges. Insufficient metadata is not labeled
unrelated. Small-panel and tie ranges are descriptive, not confidence intervals
or evidence of production efficacy. The real sampling frame, label protocol,
metric choices and resource gate still require review before a real freeze.

## Local hermetic verification

From this worktree, with only standard-library Python:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests/topical_relevance -v
```

The suite uses synthetic metadata, fake workers, a toy keyword mapping and
throwaway artifacts. There are no downloads, provider requests or real inference.
Process-tree inspection checks may need local `ps` permission.

The CLI provides `prepare --synthetic-input --text-policy --output`,
`verify --manifest`, `freeze --manifest --config --output`,
`smoke --freeze --output` (at most three synthetic candidates),
`run --freeze --output`, `resume --run-dir`, `blind-sheet --manifest --output`, and
`evaluate --run-dir --labels --output`. Paths are explicit. The approved MiniLM execution procedure is below; no model installation commands are provided.

## Approved MiniLM integration smoke (SDLC execution)

The new approval permits integration of the verified MiniLM runtime, independent
QA, commit/push of this branch, and a three-paper synthetic Mini smoke. SDLC owns
commit/push and execution. This does not deploy production or enable real-corpus
quality evaluation. SmolLM2, BGE and other learned runtimes remain disabled.

`config/minilm-smoke-papers.json` contains exactly three distinct authored
synthetic abstracts: platform governance, incident diagnosis, and tool-agent
recovery. Each receives the same three frozen interests; nine independent raw
logits aggregate by max. These are integration fixtures, not labeled quality
anchors or held-out evaluation data.

The adapter pins `cross-encoder/ms-marco-MiniLM-L6-v2` revision
`233902d25c440f23af6f7d6e94d2946bac0bee0a`, generic FP32 `onnx/model.onnx` SHA256
`5d3e70fd0c9ff14b9b5169a51e957b7a9c74897afd0a35ce4bd318150c1d4d4a`, Python 3.11,
onnxruntime 1.23.2 and transformers 4.57.6. It uses CPUExecutionProvider,
sequential execution, two intra-op threads and one inter-op thread. Scores are
raw single logits, not calibrated probabilities. No sigmoid or cross-method
score normalization is applied.

The existing volume must expose `onnx/model.onnx`, `config.json`,
`tokenizer.json`, `tokenizer_config.json`, `special_tokens_map.json`, and
`vocab.txt` beneath `/models/minilm`. Missing files stop the smoke; do not
substitute another model or download replacements. All six files are hashed
into the freeze, together with the resolved local image ID and runtime details.
Tokenizer/config hashes identify the actual local bytes; revision metadata alone
is not independent proof of tokenizer provenance. The model hash is independently
pinned to the previously verified artifact.

Freeze preflights all nine full selected text pairs, with special tokens,
`truncation=False` and no padding. Every pair must fit the smaller of 512 and the
tokenizer's declared maximum. The worker rechecks tokens and reports counts;
results must match frozen counts. Unusable abstracts still produce no inference.
Runtime/artifact fingerprints are compared again at run verification and worker
startup. Learned freezes must be verified in the same container runtime.

After QA and push, use a **separate checkout of the exact approved v2 commit** on
the Mini. Do not change the running production checkout. SDLC substitutes the
verified 40-character commit SHA in this command:

```bash
bash /home/devitolo/workspace/paper-agent-v2-model-evaluation/experiments/topical_relevance/run_minilm_smoke.sh \
  /home/devitolo/workspace/paper-agent-v2-model-evaluation \
  APPROVED_40_CHARACTER_V2_COMMIT_SHA
```

The script resolves the already installed image
`paper-agent-minilm-smoke:onnx-1.23.2` to its immutable local `sha256:` image ID
and rejects any ID except the verified
`sha256:7d8b960220e3c6f60292e6d40a8f300ff19c5ee05cd97cf5f725a76673e2d5c2`.
It passes that exact ID to Docker with `--pull=never`. It mounts only the
standalone experiments directory read-only at `/harness/experiments`, the
existing `paper-agent-minilm-model-cache` volume read-only at `/models`
(the volume itself contains the `minilm/` directory),
and a new mode-700 directory under `$HOME/paper-agent-private` at `/output`.
No production database or configuration is mounted. Artifacts include capture
date, deployed commit, image/container IDs, immutable freeze/checkpoints/summary,
container exit state, and stdout/stderr. Outputs remain outside Git.

The container has two CPU, 4 GiB RAM, a matching memory-swap limit (zero extra
swap), no network, a read-only root, a 64 MiB temporary filesystem, and no Linux
capabilities. An outer 900-second wall limit covers preparation, freeze, model
load and inference together; on timeout or interruption the trap kills and
removes the container while preserving its logs/state. The inner worker has a
120-second load limit, 60-second pair limit and sampled 3 GiB process-group RSS
limit. Linux RSS sampling uses `/proc` and needs no procps installation. The host cgroup enforces the 4 GiB ceiling for all container processes.
No retries or automatic second run occur. A successful exit requires three
complete records and exactly nine model pair calls.

Prerequisites are the already approved image/cache, readable model files for the
invoking UID, Docker, Bash, Git, realpath and GNU timeout on the Mini. No install
commands are included. Report failures and preserved artifacts to SDLC rather
than changing model, runtime, input, or resource limits during the smoke.
