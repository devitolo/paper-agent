# Mini PDF failure isolation and CORE refill

Publisher PDF URLs can contain unescaped spaces and Unicode. Reviewer encodes raw
path/query text for HTTP, preserves existing percent escapes and query ordering,
and rejects malformed authorities, controls, credentials and non-HTTP schemes.
Expected network/protocol failures return through the existing abstract-only
fallback (or explicit per-paper error when no abstract exists). Programming and
storage errors still propagate. Malformed HTML links are skipped; repeated links
and HTML fallback traversal are bounded to four distinct URLs.

CORE retains its **three HTTP attempts per Scout fetch**, including retries. A
page containing only duplicates or too-old works can continue at its provider
offset while the candidate target remains unfilled. A multi-query fetch allows
at most two successful pages for one query; a single-query fetch can use the
three-attempt budget. Least-recently-served query state persists, so deferred
queries rotate first on the next fetch/run.

This budget cannot cover three initial query pages plus a continuation. A refill
can therefore defer one query; it does not increase provider traffic. The budget
also includes transient retries. No Semantic Scholar-specific pagination or key
logic is reused.

Freshness samples and their same-fetch continuations keep a separate traversal;
they update last_refresh without resetting the deep checkpoint. Later fetches
resume the deep offset. Successful page dispositions and checkpoints still commit
atomically with Scout candidates. End-of-results, repeated pages, non-advancing
offsets, cooldown, candidate target and shared request/time limits stop retrieval.
The time window derives from the existing three request timeouts and two normal
delays; Retry-After waits that cannot fit defer rather than extend it.
This is request admission, not a wall-clock watchdog around response streaming;
the existing socket and outer job timeouts still apply. The four-URL PDF limit
bounds traversal count, not individual response body size.

Regression tests exercise the actual CORE transport and Scout checkpoint path,
all-query budget sharing/rotation, filtered-page refill, freshness/deep separation,
exhaustion/repetition, 429 and retry-time limits. Reviewer tests cover the exact
publisher path shape, Unicode, signed-query preservation, malformed links, failure
fallback/skip and continued processing of the next recommendation.

No schema, provider credentials, schedules, deployment scripts or model settings
change. Preparing and pushing this branch does not authorize production deployment
or a source discovery rerun.
