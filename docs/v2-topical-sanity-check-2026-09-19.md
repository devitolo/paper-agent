# V2 topical relevance synthetic sanity check

Date: 2026-09-19  
Status: development evidence; no production adoption decision

This report records a bounded comparison run directly by SDLC on the Mini. It
separates four stages that must not be conflated:

1. **MiniLM feasibility:** the earlier three-paper/nine-pair integration smoke at
   commit `9085fe821492c1ff69b575f327e3096c2625a3c2` established that the pinned
   local ONNX runtime works within its isolated harness. Thirty-four hermetic
   tests passed independently.
2. **Synthetic sanity check:** the present nine-paper comparison asks whether
   MiniLM and SmolLM2 produce a useful ordering on deliberately simple authored
   examples. It is development evidence only.
3. **Exploratory SmolLM2 diagnostic:** post-result prompt variants probe the
   all-tied output. They are not independent quality evidence.
4. **Future blind real-paper evaluation:** MiniLM and the topic-only keyword
   baseline should next be compared on about 30 real, previously unreviewed
   papers. That evaluation has not happened.

Exact fixtures, fixed expectations, per-interest scores, timings, token counts,
rankings, and diagnostic responses are preserved in
[`docs/evidence/v2-topical-sanity-20260919/`](evidence/v2-topical-sanity-20260919/README.md).
No weights, production data, or private real-paper corpus are included.

## Method

The authored panel contains three relevant, three partial, and three unrelated
papers. Expectations were written before inference. Every model received the
same title, abstract, and each of these interests:

1. enterprise AI platform/architecture/governance/organizational tradeoffs
2. AIOps/observability/incident response/diagnosis/RCA/logs-metrics-traces/system relationships
3. agent reliability/tool-agent coordination/context/failure handling/recovery/human oversight

Each paper's overall score is the maximum of its three interest scores. Labels
and expected target interests were not sent to either model. Both runs completed
27 comparisons, exited successfully, and did not exhaust the 4 GiB limit.
Containers were offline, limited to two CPUs and 4 GiB, and mounted existing
model caches read-only. Stable `main` and production scoring were not modified.

### MiniLM setup

- `cross-encoder/ms-marco-MiniLM-L6-v2`, revision
  `233902d25c440f23af6f7d6e94d2946bac0bee0a`
- Generic FP32 `onnx/model.onnx`, SHA-256
  `5d3e70fd0c9ff14b9b5169a51e957b7a9c74897afd0a35ce4bd318150c1d4d4a`
- Python 3.11, ONNX Runtime 1.23.2, Transformers 4.57.6,
  `CPUExecutionProvider`, sequential execution, two intra-op threads, one
  inter-op thread
- Interest paired with title, newline, and abstract; special tokens, no padding,
  no truncation; every pair below 512 tokens
- One raw logit per pair; no sigmoid, calibration, or cross-method normalization

### SmolLM2 setup

- `smollm2:1.7b-instruct-q4_K_M`, digest
  `8ea75f835db9a9b29bb19d2740ea8217e3027e687e5f66b42fd44ff9cbaa4cb9`
- Ollama 0.32.4, CPU-only, one loaded model, serial requests
- System prompt: score topical relevance only; ignore novelty, usefulness,
  evidence quality, and recency. User JSON: interest, title, abstract only
- Temperature 0, seed 42, two threads, 2048 context tokens, 32 output tokens,
  no GPU, JSON schema requiring one integer in `0..2`
- Scale: 0 unrelated, 1 partial, 2 direct topical match

## Full score matrix

| Paper | Expected | MiniLM I1 | MiniLM I2 | MiniLM I3 | MiniLM max | SmolLM2 I1/I2/I3 | SmolLM2 max |
|---|---|---:|---:|---:|---:|---:|---:|
| Enterprise AI governance (`p01`) | relevant | 7.697307 | -10.973817 | -11.345966 | 7.697307 | 2 / 2 / 2 | 2 |
| Cloud incident diagnosis (`p02`) | relevant | -11.167158 | 2.889075 | -11.006429 | 2.889075 | 2 / 2 / 2 | 2 |
| Agent recovery (`p03`) | relevant | -10.822775 | -10.162577 | 5.919391 | 5.919391 | 2 / 2 / 2 | 2 |
| Corporate data warehouses (`p04`) | partial | -9.328519 | -11.351391 | -11.236250 | -9.328519 | 2 / 2 / 2 | 2 |
| Telemetry compression (`p05`) | partial | -11.399807 | -7.526052 | -11.415707 | -7.526052 | 2 / 2 / 2 | 2 |
| Long-context summarization (`p06`) | partial | -11.415015 | -11.230066 | -11.162723 | -11.162723 | 2 / 2 / 2 | 2 |
| Archaeological pottery (`p07`) | unrelated | -11.376270 | -11.382282 | -11.434746 | -11.376270 | 2 / 2 / 2 | 2 |
| Wheat irrigation (`p08`) | unrelated | -11.360355 | -11.457438 | -11.441073 | -11.360355 | 2 / 2 / 2 | 2 |
| Binary-star orbits (`p09`) | unrelated | -11.279210 | -11.268934 | -11.432781 | -11.268934 | 2 / 2 / 2 | 2 |

MiniLM ordered all relevant papers above all partial papers and all partial papers
above all unrelated papers. Each relevant paper scored highest against its
intended interest. Long-context summarization had only narrow separation from
the unrelated group. Raw logits are not probabilities or production thresholds.

SmolLM2 returned `2` for all 27 comparisons. This is an uninformative tie, not
successful ranking.

## Runtime evidence

| Evidence | First/cold | Warm | Memory |
|---|---:|---:|---:|
| Earlier MiniLM feasibility smoke, 9 pairs | load measured separately | median about 25 ms/pair | sampled worker process-group RSS about 199 MiB |
| Nine-paper MiniLM sanity run | load recorded in remote artifacts | median 0.02458741026930511 s/pair | whole-container cgroup peak 366,403,584 bytes |
| Earlier SmolLM2 smoke, 9 pairs | 27.532 s first request, including 16.994 s load | median 4.7629 s/request | whole-container peak 1,982,693,376 bytes (about 1.85 GiB) |
| Nine-paper SmolLM2 sanity run | 32.451212492305785 s first request | median 5.278472139732912 s/request | whole-container cgroup peak 2,329,415,680 bytes |

The MiniLM feasibility memory number is sampled worker/process-group RSS. The
SmolLM2 figures and present sanity summaries are whole-container cgroup peaks.
Those scopes differ and are not directly comparable. Cold-request timings also
include different startup work and are not normalized benchmarks.

Earlier artifacts:

- `/home/devitolo/paper-agent-private/minilm-integration-I7TGUbA4`
- `/home/devitolo/paper-agent-private/smollm2-smoke-NxE5Q1Ff`

Present artifacts:

- `/home/devitolo/paper-agent-private/sanity-minilm-7Ihux6Gk`
- `/home/devitolo/paper-agent-private/sanity-smollm2-N1lC78Ui`
- `/home/devitolo/paper-agent-private/sanity-diagnose-111FhYLU`

## Exploratory SmolLM2 diagnostic

After the tie was known, nine requests compared `p01`, `p04`, and `p07` only
against enterprise AI:

| Condition | `p01` AI platform | `p04` warehouse | `p07` pottery |
|---|---|---|---|
| JSON without enum | `{"score": 2}` | `{"score": 2}` | echoed input; 32-token length stop |
| Same prompt, no format constraint | `{"score": 2}` | `{"score": 2}` | echoed input; 32-token length stop |
| Plain-language classification | `Unrelated` | `Partial` | `Partial` |

Removing the enum or schema alone did not repair behavior. This does not identify
a definitive cause or prove that every SmolLM2 prompt would fail. More tuning on
these observed fixtures would be development work, not independent validation.

## Limitations

- Synthetic authored examples are not the production candidate distribution.
- Expectations were fixed before this run, but the panel is development data,
  not held-out evidence. The relevant items were used in the integration smoke.
- Maximum-over-interests does not test calibration, thresholds, difficult
  boundaries, malformed metadata, multilingual text, or topic mixtures.
- The result does not establish production precision, recall, usefulness,
  recommendation quality, or robustness.
- SmolLM2 had one bounded prompt/decoding setup; no model-family claim follows.
- No topic-only keyword result exists yet, so MiniLM has not beaten the baseline.

## Recommendation

Advance MiniLM and the topic-only keyword baseline to one blind evaluation on
approximately 30 real, previously unreviewed papers. Freeze the sample,
interests, keyword mapping, text policy, metrics, and tie handling before
inference; label without scores; report failures and insufficient metadata.
Set SmolLM2 aside for now.

This is not authorization for production adoption, thresholds, ranking changes,
rollout, or broader V2 scope.
