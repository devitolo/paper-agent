# First-pass keyword versus Qwen: current QA scope

Latest user scope via architect: implement and test the minimal runnable comparison
of existing keyword relevance against local Qwen on the 15 frozen metadata records.
No ChatGPT implementation, export/import, hybrid, repeat pass or PDF dependency.
Older PDF/selector/V4 changes stay paused and preserved. No extra design gate.

Acceptance checks:

- End-to-end runner produces per-paper existing raw keyword score and Qwen relevance
  score/unknown, deterministic rank, rationale and observed runtime.
- Judge input uses title/abstract and frozen current profile context, with historical
  topic when available. Missing provenance is flagged and does not block execution.
- Direct labels, ratings, reviews, old scores and paper-specific profile notes never
  enter judge requests. Labels may join only after judgment for comparison.
- Maximum 15 serial local calls, 45-second hard maximum per call, zero retries;
  runner also has a finite overall deadline. First pass only; repeats deferred.
- Invalid score types/ranges, booleans, nonfinite numbers, missing metadata, malformed
  output and timeouts are explicit unknown/failures, not zero relevance by default.
- Actual latency/usage reported; unavailable usage unknown. Private exception bodies
  and credentials excluded. No paid/external fallback or automatic model download.
- Input corpus/profile bytes immutable; output aliases protected; no production DB
  writes, ranking activation, source calls or PDF processing.
- Existing keyword scores retain their raw scale; no false semantic normalization.
  Current profile identity/hash and feedback-informed bias disclosed where available.

Developer sends one ready code/entrypoint handoff or identifies a concrete blocker.
QA verifies it with injected responses and temporary inputs/outputs; no live model
calls by QA. Passing engineering tests means executable readiness, not demonstrated
model quality or generalization on 15 previously seen development papers.
No commit/push/deployment authorization is supplied by this handoff.
