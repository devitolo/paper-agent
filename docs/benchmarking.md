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
