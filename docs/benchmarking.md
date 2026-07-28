# Benchmarking

Project Paper should choose providers, prompts, and local models based on measurements from the real workflow.

## Cloud Scouting Benchmark

Track:

- Date
- Topic
- Provider
- Model or client
- Quota before
- Quota after
- Search queries used
- Candidates returned
- Unique candidates
- Candidates remaining after history filtering
- PDFs downloaded
- Useful papers
- User-rated high-quality papers
- Runtime
- Errors
- Notes

Template:

```text
Date:
Provider:
Model or client:
Topic:
Quota before:
Quota after:
Queries:
Candidates:
Unique candidates:
Remaining after history filtering:
Downloaded:
Useful papers:
High-rated papers:
Runtime:
Errors:
Notes:
```

## Local Model Benchmark

Track:

- Model
- Quantization
- File size
- Peak RAM
- CPU utilization
- Startup time
- Tokens per second
- Time per abstract
- Time per paper
- JSON validity rate
- Scoring agreement with user
- Summary quality
- Failure rate

Template:

```text
Model:
Quantization:
File size:
Peak RAM:
CPU utilization:
Startup time:
Tokens per second:
Time per abstract:
Time per paper:
JSON validity rate:
Scoring agreement with user:
Summary quality:
Failure rate:
Notes:
```

## Evaluation Tasks

Use a fixed benchmark set containing examples of:

- High-interest papers
- Low-interest papers
- Previously accepted papers
- Previously declined papers
- Near duplicates
- Papers with misleading titles
- Papers with useful practical results
- Papers that are too theoretical

Do not choose a local model based only on public benchmarks. Public benchmarks can narrow the candidate list, but final selection should come from the actual Project Paper workload on the Mac mini.

## Mac Mini Initial Local Model Results

Test host:

- Mac mini Late 2012
- Xubuntu 24.04 LTS
- Ollama local runtime
- Docker installed for worker execution

Benchmark paper:

- arXiv: 2301.03797
- Title: Recommending Root-Cause and Mitigation Steps for Cloud Incidents using Large Language Models
- PDF text extracted with `pdftotext`

Task:

- Extract structured paper metadata from a paper excerpt.
- Initial benchmark returned JSON with `problem`, `why_hard`, `proposed_use_of_llms`, `dataset_or_scale`, and `evaluation_method`.
- Prefer grounded extraction over fluent summarization.

| Model | Size Class | Short Excerpt Result | Long Excerpt Result | Runtime Observed | Strengths | Failure Modes | Recommendation |
|---|---:|---|---|---:|---|---|---|
| `qwen2.5:0.5b-instruct` | 0.5B | Roughly on-topic, but weak instruction following | Not worth continuing | Fastest, not formally timed in final run | Good smoke test for Ollama/Qwen | Failed exact bullet count, failed JSON, vague/generic, hallucination risk | Remove/use only as health check |
| `qwen2.5:1.5b-instruct` | 1.5B | Good schema adherence, captured problem/scale/eval, missed `why_hard` | Best long-context result; kept requested keys and mostly grounded | ~58s short, ~135s long | Best reliability balance, stable JSON mode, grounded extraction | Sometimes leaves fields `null`, folds dataset into evaluation, conservative/misses obvious facts | Best default local extractor |
| `qwen2.5:3b-instruct` | 3B | Not tested on short 80-line excerpt | Very slow and hallucinated a specific VM/workspace incident | ~298s long | Captured dataset/evaluation detail | Too slow; latched onto example incident and mistook it for paper problem | Not worth it on this Mini |
| `gemma3:1b` | 1B | Best short-excerpt result; filled all fields and was faster than Qwen 1.5B | Drifted badly: omitted keys, became chatty, included Spanish/markdown inside field | ~53s short, ~89s long | Strong short-context summarization, fast | Schema drift on longer context, multilingual glitch, verbose field stuffing | Good for short snippets, not default extractor |
| `llama3.2:1b` | 1B | Valid compact JSON, but reframed problem as risks of using LLMs | Long excerpt not tested | ~70s short | Obeyed JSON format, decent compactness | More inferential, less faithful to paper framing | Worth one long test, likely secondary |

Current choice:

Use `qwen2.5:1.5b-instruct` as the default local extractor on the Mac mini.

Current triage schema:

```json
{
  "paper_date": null,
  "research_problem": null,
  "why_it_matters": null,
  "approach": null
}
```

This schema is intentionally simpler than the initial benchmark schema. It supports the first review decision: whether the paper is interesting enough to inspect further.

It is not perfect, but it was the most stable model on longer excerpts and the easiest to wrap with validation and retry logic. Treat it as a cheap local first pass, not a final judge.

Recommended local extraction loop:

1. Extract PDF text with `pdftotext`.
2. Split text into chunks.
3. Ask Ollama/Qwen for strict JSON using `format: "json"`.
4. Parse and validate the response.
5. Retry once with a repair prompt when JSON is invalid or required fields are missing.
6. Save raw chunk outputs plus merged paper-level output.
7. Use a stronger cloud model only for final synthesis or difficult papers.

## Mac Mini Worker Concurrency Results

Test host:

- Mac mini Late 2012
- Intel Core i7, 8 GB RAM
- Xubuntu 24.04 LTS
- Ollama with `qwen2.5:1.5b-instruct`

Benchmark command shape:

```bash
time python3 -m paper_agents.cli extract ~/qwen-paper-test/incident-llm.pdf \
  --model qwen2.5:1.5b-instruct \
  --limit-chunks 0 \
  --workers WORKER_COUNT \
  --output /tmp/incident-workersN-fixed.json
```

Benchmark paper:

- arXiv: 2301.03797
- Full PDF extraction
- 13 chunks
- Triage schema: `paper_date`, `research_problem`, `why_it_matters`, `approach`

| Workers | Runtime | Merge Strategy | Output Quality | System Notes |
|---:|---:|---|---|---|
| 1 | 26m22s | Initial run predates fallback merge fix | Poor final merge in initial run: blank strings for main fields | Usable but extremely slow |
| 2 | 4m05s | `synthesis` | Best result: concise, grounded, captured service health and developer productivity | RAM about 2.52 GB / 7.66 GB, swap nearly unused, SSH/VNC remained usable |
| 3 | 4m18s | `synthesis` | Acceptable but slightly more generic than workers 2 | Higher load, no speed benefit over workers 2 |

Best observed settings:

```bash
time python3 -m paper_agents.cli extract ~/qwen-paper-test/incident-llm.pdf \
  --model qwen2.5:1.5b-instruct \
  --limit-chunks 0 \
  --workers 2 \
  --output /tmp/incident.json
```

Workers 2 is the current default recommendation for the Mac mini. It delivered roughly a 6.4x wall-clock improvement over the single-worker baseline on this paper, while preserving better output quality than workers 3 and keeping the machine responsive.

