# OpenAlex cursor retrieval trial

Status: implemented locally, opt-in; not enabled in production. Semantic Scholar,
arXiv, query text, ranking, and duplicate exclusions are unchanged.

Enable `PAPER_OPENALEX_CURSOR=1` **inside the application process** after deploying
this code and applying the additive schema through the normal `init-db` path.
`ScoutConfig(openalex_cursor_enabled=True)` enables the same path in Python.
Setting a host environment variable alone does not pass it into an existing
container. Production image integration and rollout must preserve the migration
assets described in `mini-production-operations.md`; a native checkout pull does
not update the production container. Disable the flag to return to legacy fetch;
keep the new tables rather than dropping checkpoints.

## Trial policy

- Three metadata-search HTTP attempts per Scout run; scheduled OpenAlex uses one
  Scout attempt. No internal retries in the cursor transport. Manual workflows
  with multiple Scout attempts can consume three per attempt. This is not a cap
  on unrelated pipeline enrichment/model calls.
- Up to ten works per page, reduced to remaining configured candidate capacity.
  At most one page per distinct effective query per run. Least recently attempted
  queries go first; deferred queries appear in diagnostics. Configured request
  spacing and timeout still apply. Retry count does not affect this path.
- A 429 ends retrieval and saves a provider-wide not-before timestamp. Numeric
  and HTTP-date Retry-After values are honored. Missing/invalid values use
  60–90 seconds. No immediate retry. Cooldown is saved even if later storage fails.
- Query identity includes endpoint, constructed search, type filter, sort,
  freshness policy and contract version. A traversal freezes its inclusive lower
  and upper publication-date bounds; it expires after 30 days. It is not a
  provider snapshot: changing indexes can still produce duplicates or omissions.
- After seven days, a query receives a bounded first-page freshness check against
  the current date window. The next visit resumes its deep cursor. A refresh can
  miss new arrivals beyond that first page; this is a conservative trial policy,
  not a completeness guarantee.
- HTTP 400 on a continuation resets that traversal for a later run, without an
  immediate retry. Diagnostics identify HTTP 400; they do not assert the token
  was definitely invalid. Other errors preserve cursor progress.
- No SQLite write lock is held across HTTP. At storage time, optimistic state
  validation rejects a concurrent checkpoint. All accepted candidates, raw
  per-query page membership/rejection dispositions, and cursor changes commit
  together. Candidate-save failure rolls them all back. The separately saved
  cooldown survives. Cursor replay is safe through existing deduplication.

`openalex_search_state` holds progress and provider cooldown.
`openalex_page_dispositions` holds whole returned works and per-item dispositions
linked to the Scout run, including cross-query duplicates and filtered works.
This ledger grows with retrieval; retention is a separate future operation, not
an automatic deletion policy in this trial.

## Diagnostics and acceptance

Source diagnostics distinguish request/candidate budget, query-round completion,
source error, cooldown, and exhausted traversals awaiting refresh. Provider end
is determined from continuation metadata, never from duplicate-only results.
Per-page counts include raw items, distinct IDs, accepted/rejected items, and
previously unseen canonical keys relative to the database before the run. These
are not topical-relevance judgments. Actual stored eligibility and exclusion
reasons remain in the normal Scout report. The whole-page ledger preserves raw
membership before cross-query deduplication.

The first live check should remain retrieval-only: same query and filters,
first page versus its next cursor, bounded requests, database opened read-only,
no Curator/Qwen calls and no recommendations. Then evaluate scheduled runs for
cursor advancement, unseen candidates/request, deferred queries and rate limits.
Do not interpret a day with no eligible papers as provider exhaustion.

Official cursor contract: https://help.openalex.org/api/paging/ (verified during
implementation): start with `cursor=*`, follow `meta.next_cursor`, supported
`per_page` maximum 100. This trial uses at most 10.
