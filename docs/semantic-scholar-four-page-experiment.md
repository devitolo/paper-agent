# Semantic Scholar four-page experiment

Status: release candidate for the explicitly authorized Mini activation. Offline
validation only so far; no production discovery rerun or live provider requests.
Base: mini-production release 4ec18b4 (2026-10-27).

## Contract

This experiment is explicitly selected by Python callers using both
`ScoutConfig(semantic_scholar_progress_enabled=True,
semantic_scholar_four_page_experiment=True, ...)`. Both flags default to false.
Alternatively, with existing progressive Semantic Scholar mode active, the explicit
environment flag `PAPER_SEMANTIC_SCHOLAR_FOUR_PAGE_EXPERIMENT=1` enables the experiment
only in the Semantic Scholar branch. The new flag defaults to zero in Compose.
The authorized Mini production workflow sets its updater input
`PAPER_MINI_SEMANTIC_SCHOLAR_FOUR_PAGE_EXPERIMENT=1`; the updater persists that value
in the private production env file, verifies the running container value, and backs
up/restores the original env on rollback. Input `0` explicitly disables it. No new
cron or CLI switch is introduced. Other sources are unaffected; default retrieval
without the flag retains its three-attempt budget and query pass.

The experiment follows provider `next` offsets within the same Scout run:

- At most four successful pages and four total actual HTTP attempts, globally across
  all selected queries. The four-attempt allowance was explicitly approved for this
  experiment; it does not replace the production default of three.
- Pages one and two use key1; if a third page is needed, switch to key2 for pages
  three and four. Existing early failover after two key1 HTTP429s is retained, and
  never switches back to key1. A planned page-three switch is recorded separately
  from a rate-limit failover. Only slot labels enter diagnostics, not key values.
- Retries and key failover consume the same four actual HTTP attempts. For example,
  a transient failure plus three successful pages exhausts the request allowance.
- Stop on a new-paper page, four pages, four attempts, provider exhaustion, repeated
  page contents within a query, non-advancing cursor, cooldown, or terminal HTTP429.
  A new-paper page is processed in full; this is not a one-paper truncation.
- The provider-exhaustion stop ends this bounded experiment, even if other queries
  remain. Their selection order is preserved for subsequent runs.
- A queue gives other selected queries their first turn before revisiting an active
  query. The existing persisted last-served ordering continues across runs.
- The one-page weekly freshness sample does not overwrite the saved deep cursor.
  A queued revisit then resumes that deep traversal, not a new freshness traversal.
- A repeated page is recorded in the dispositions ledger but does not advance its
  checkpoint past that repeated response. Invalid cursors are rejected by the HTTP
  adapter before acceptance, with a specific stop reason.
- Existing atomic cursor/page/candidate checkpoints and persisted terminal cooldown
  remain in force. An active persisted cooldown causes zero HTTP requests.

Time is also bounded for the experiment: a shared monotonic allowance of
`min(240 seconds, 4 * source.timeout)` is set for the fetch. It is checked between
requests/retries, clamps each urllib socket timeout, and prevents a Retry-After
sleep from consuming the remaining allowance. This does not replace the caller's
outer whole-job timeout; urllib's in-flight socket timeout is not an independent
hard process deadline. No production timeout is changed.

## Tests and evidence

`tests/test_semantic_scholar_four_page_experiment.py` uses temporary SQLite and the
actual `SemanticScholarSource.fetch_page` adapter with intercepted `urlopen` calls.
It checks emitted query offsets and key-slot header selection, real request-attempt
accounting, and persistent state. No public provider is contacted.

Coverage includes same-run duplicate→new discovery, four duplicate pages and the
key1/key1/key2/key2 plan, transient retries, early key2 failover, both-key429 cooldown,
repeated/non-advancing pages, provider exhaustion, fair query order, bounded sleeps,
atomic rollback, next-run cursor persistence, freshness/deep separation, malformed
payload attempt accounting, and unchanged default behavior.

The compatibility suite includes existing Semantic Scholar progress, key-failover,
cooldown, environment wiring, Scout diagnostics and Mini release-pipeline tests.
Passing these offline cases is not proof of live provider yield or quota behavior.
Mini activation for the next scheduled Semantic Scholar run is authorized. No
manual live provider experiment or production rerun is included in this release.

## History behind the gap

`1da2bd0` (2026-09-06) implemented generic Scout refill using increasing search
limits. `77830c7` (later on 2026-09-06) explicitly capped Semantic Scholar at one
conservative generic fetch round, renamed its two-round test, removed the seeded
duplicates and changed the expected limits from `[10, 20]` to `[10]`. The original
Semantic refill intent was therefore already narrowed before progressive pagination.
`b68ced0` (2026-09-26) then added a progressive Semantic Scholar path that bypassed
that generic loop, visited each query once and saved its offset for the next run.
The shipped duplicate-page test called Scout twice; it demonstrated cross-run
resume, not duplicate-triggered same-run continuation. This experiment exercises
the missing behavior directly through the HTTP adapter and SQLite checkpoint.


Validation command (offline fixtures only):

```sh
PYTHONPATH=.:tests python3 -m unittest \
  tests.test_semantic_scholar_four_page_experiment \
  tests.test_semantic_scholar_progress tests.test_semantic_scholar_key_failover \
  tests.test_semantic_scholar_failover_qa tests.test_semantic_scholar_env_wiring_qa \
  tests.test_semantic_scholar_cooldown tests.test_scout_diagnostics \
  tests.test_mini_release_pipeline -q
```

Result: 121 tests passed, including 19 new experiment acceptance cases;
`git diff --check` passed. Existing SQLite ResourceWarnings are non-failing.

Deployment readiness follow-up: the first release attempt did not become ready in
its 120-second window and restored the prior app. Independent pipeline readiness
confirmed the prior app healthy, despite the old rollback probe incorrectly using
a web-only check. The updater now re-reads the current runtime role on every probe,
uses pipeline readiness for pipeline containers and web readiness for UI containers,
and allows a bounded 300-second startup/recovery window. The generated manual
rollback script uses the same probe, which also works with the prior image.
