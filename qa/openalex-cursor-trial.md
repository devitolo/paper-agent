# OpenAlex cursor trial evidence

Production baseline inspected: container `paper-mini-production-app-1`, image
`sha256:817716e84db53235c0ec082323880f3af4c2e62843699749336b84ff89115eb9`.
The container still has the legacy ten-per-topic first-page cap.

A separate Python process in that container ran the candidate `fetch_page`
method, without changing files, services, environment configuration, or database
records. Its database connection used SQLite `mode=ro`. Two HTTP attempts,
five-second spacing, twenty-second per-request timeout, no retries/model calls.
Latest recorded OpenAlex baseline was run 256. Query:
`aiops and ai-driven site reliability engineering (sre)`; existing query context
and type filter retained, 24-month window bounded through the probe date.

| Metric | First page | Second page via returned cursor |
|---|---:|---:|
| Raw works | 10 | 10 |
| Distinct IDs | 10 | 10 |
| IDs overlapping previous page | 0 | 0 |
| Accepted works absent from database | 0 | 1 |
| Further cursor available | yes | yes |

The unseen title was “Context-Aware Multi-Agent Root Cause Analysis for Distributed
Transaction Failures”. This establishes useful pagination evidence for one query,
not topical quality, general recall, or daily recommendation yield. No production
cursor state was written. This transport-only probe does not qualify the complete
coordinator inside the production image; local integration tests cover storage.

Deployment remains disabled pending integration into the container release.

## Local verification

- Focused cursor/integration suite: 21 tests passed.
- Full suite: 363 tests run, 17 skipped, no failures (69.350 seconds).
- Initial sandboxed full run could not run `ps` in ten existing Gemini cleanup
  subtests; an approved unrestricted rerun passed. Existing database-resource
  warnings appeared. Final focused rerun also passed after guarding missing
  Retry-After headers.
- `git diff --check` passed.

Commands:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p test_openalex_progress.py -q
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -q
```
