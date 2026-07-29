# Workflow

This document describes the planned end-to-end workflow. The current implementation includes arXiv discovery, deterministic ranking, PDF download for selected candidates, local Ollama triage extraction, SQLite run/artifact registration, OpenAI-assisted review generation, and JSON-profile feedback.

## Scheduled Run

1. A systemd timer starts the workflow.
2. The workflow loads configuration, paper history, and user preference data.
3. The selected cloud provider scouts for candidate papers.
4. Candidate records are normalized.
5. Local deterministic filters remove:
   - Exact duplicates
   - Previously declined papers
   - Already accepted papers
   - Recently reviewed papers
   - Duplicate versions of the same work
6. Open-access PDFs are downloaded automatically.
7. Downloaded files are hashed and registered.
8. PDF text is extracted.
9. Relevant sections are selected, such as:
   - Title
   - Abstract
   - Introduction
   - Methodology
   - Results
   - Conclusion
10. A local model scores and summarizes the selected content.
11. Strong candidates may be escalated to a cloud model.
12. A report is generated for user review.
13. User scores, accepts, or declines recommendations.
14. Feedback is written to the local database and used in future runs.

## Paper Identity

Use the following identity priority:

1. DOI
2. arXiv ID
3. OpenAlex or Semantic Scholar ID
4. Normalized title and authors
5. PDF hash

## Feedback States

Support at least:

- `unseen`
- `discovered`
- `downloaded`
- `locally_scored`
- `recommended`
- `accepted`
- `declined`
- `archived`

Optional feedback fields:

- User score
- Decline reason
- Positive tags
- Negative tags
- Notes

## Preference Reuse

Do not send the complete history to the cloud model on every run.

Instead:

1. Filter exact matches locally.
2. Retrieve only relevant positive and negative examples.
3. Generate a compact preference profile.
4. Include only that compact profile in the scouting or ranking request.

The existing `data/profile.json` is a useful seed for the compact preference profile, but it is not a replacement for paper-level history.


## Local Output Conventions

Use stable folders so scheduled runs are easy to inspect and sync:

- Scout metadata: `data/scout/YYYY-MM-DD.jsonl`
- Downloaded PDFs: `data/papers/<source>/`
- Local extraction summaries: `data/extractions/<source>/`
- ChatGPT section-by-section reviews: `data/reviews/<source>/`
- SQLite registry: `data/paper_agent.db`

When a downloaded arXiv PDF is extracted, deterministic arXiv metadata should supply the paper date before falling back to model-extracted dates.

## SQLite Registry

The registry currently stores:

- `papers`: normalized source metadata and stable source identifiers.
- `scout_runs`: source, topics, mode, fetch/keep limits, and run time.
- `scout_candidates`: candidate score, matched keywords, ranking reason, and whether the candidate was selected.
- `artifacts`: downloaded PDFs, triage summaries, and later generated review files.
- `feedback`: reserved for Feedback Loop v2.

Useful inspection commands:

```bash
python3 -m paper_agents.cli db stats
python3 -m paper_agents.cli db recent-runs --limit 5
python3 -m paper_agents.cli db papers --selected --limit 10
```

## Source Reliability

The arXiv source adapter supports polite request tuning:

```bash
python3 -m paper_agents.cli scout-daily \
  --topic "incident management" \
  --fetch 3 \
  --keep 1 \
  --no-download \
  --request-delay 5 \
  --retries 4 \
  --source-timeout 90
```

If one topic fails but another succeeds, the run keeps the successful candidates. If every topic fails, the command exits with a diagnostic error and does not write an empty Scout result.
