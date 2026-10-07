# V2 topical sanity-check evidence

This directory preserves the non-private synthetic evidence for the 2026-09-19
comparison described in
[`../../v2-topical-sanity-check-2026-09-19.md`](../../v2-topical-sanity-check-2026-09-19.md).

- `inputs.json`: three interests and nine synthetic title/abstract pairs.
- `expected.json`: expectations fixed before inference.
- `minilm-results.json`: 27 raw logits, exact timings, token counts, max scores,
  ranking, warm median, and whole-container peak.
- `smollm2-results.json`: 27 integer outputs with the same runtime fields.
- `diagnostic-results.json`: nine exploratory post-result SmolLM2 responses.

These are evidence snapshots, not runnable production configuration. Model
weights, private Mini logs, raw server responses, and real paper corpora are
excluded. The report records immutable model/runtime identifiers and remote
artifact locations needed to interpret the snapshots.
