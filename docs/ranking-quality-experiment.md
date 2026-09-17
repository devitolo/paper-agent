# Offline contribution-aware ranking experiment

Status: proposed and gated. Production Curator remains `evidence-aware-v3`.
Nothing in this experiment activates a profile, rewrites feedback, changes a
recommendation, calls a source/provider, or alters scheduled behavior.

The proposal separates current-topic fit from contribution-appropriate rigor.
Empirical and systems work can receive rigor credit only from grounded
measurements and comparisons. Architecture, framework, perspective, survey and
theoretical work can receive credit from grounded substantive reasoning,
tradeoffs, actionable implications and limitations. Missing, uninspected,
truncated, malformed or ungrounded evidence receives no positive credit. A
missing experiment is not itself a rejection of a non-empirical contribution.

Targeted evidence selection inspects bounded passages associated with method,
evaluation, limitations and conclusion. It records exact character offsets,
selection reasons, selected character counts, heading/fallback behavior and
truncation. These fields describe selected excerpts only; they never assert that
the complete paper or a complete section was inspected.

## Frozen input contract

The replay fixture is JSON with:

- `schema_version: 1`, a corpus identifier, immutable candidate records and a
  matching `candidates_sha256`;
- a profile snapshot and matching `profile_sha256`;
- explicit development/heldout/unassigned partitions;
- stored labels preserving `decision` separately from `user_score`;
- fixed budgets of at most 7,000 total characters, 2,000 characters per
  section class and eight passages;
- stored proposed assessments. Replay never invokes a model or network service.

Known anchors—including the rejected Future of SRE paper and the 3/5-kept
From Pilots to Platforms paper—are development examples and must not be placed
in heldout. Titles and identifiers are fixture metadata, not ranking rules.

Run a frozen replay with:

```bash
python3 -m paper_agents.ranking_quality_replay frozen-corpus.json \
  --output replay-report.json
```

The report includes input/profile hashes, versions, budgets, zero model/network
call counts, per-paper baseline/proposal components and deltas, partitioned
ordering, high-ranked rejections, missed useful papers and runtime. A contract
fixture proves behavior only. Quality improvement requires a reviewed corpus,
predeclared denominators and an untouched heldout set.

## Minimal sanitized Mini export

The local development database has no reviewed papers. Export the Mini corpus
read-only, explicitly assigning known examples to development and freezing any
genuinely untouched papers as heldout:

```bash
cd ~/workspace/paper-agent
python3 scripts/export_ranking_quality_corpus.py \
  --db data/paper_agent.db \
  --assign-unlisted development \
  --output /tmp/ranking-quality-corpus.json
```

Historical feedback has already influenced the active profile, so the safe first
export assigns all existing labels to development and makes no heldout-quality
claim. For a future untouched set, omit `--assign-unlisted development`, repeat
`--development-paper-id` and `--heldout-paper-id` explicitly, and freeze all
assignments before tuning. Papers not explicitly assigned remain `unassigned`;
replay rejects them until they are frozen. The export
omits raw feedback, parsed observations, authors, URLs and local artifact paths.
It includes titles, abstracts, keep/reject, numeric score, the active profile,
and bounded stored triage text where available. Stored summaries or abstracts
remain limited evidence and do not establish full-paper inspection.
