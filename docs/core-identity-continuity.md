# CORE identity continuity

A CORE source ID can be rediscovered with a DOI that was absent on the original
record. Creating a new DOI-canonical row and retargeting that source alias breaks
the identity AWS already owns. Ingestion must preserve the existing paper ID and
canonical key, including all reviews, feedback, recommendations and artifacts.

The shared read-only resolver checks source alias ownership and canonical/DOI/
arXiv claims before upsert. It reuses one unambiguous existing row and keeps its
canonical key. Multiple owners or contradictory stored identifiers raise an
explicit identity conflict. Source upsert also rejects retargeting an alias. The
AWS receiver's independent source-alias safety check is unchanged.

CORE uses this resolution before classifying a candidate as new. Known aliases
and cross-source DOI matches are duplicates even if the incoming canonical key
would differ. These records do not become new eligible candidates/recommendations.
Within-fetch source ID tracking prevents a DOI addition from creating a second
candidate. Contradictory identifier claims for the same source ID within one fetch
withhold its checkpoint rather than silently choosing an identifier.

Known duplicates can fill missing DOI/arXiv fields and add unambiguous source aliases
inside the existing atomic Scout checkpoint/candidate transaction. Enrichment does
not overwrite existing rich title/abstract/author metadata or historical rows.
Identity is revalidated at checkpoint. Any write/conflict failure rolls back the
identifier refresh, source aliases, page ledger and cursor advancement together.
HTTP remains outside the SQLite write transaction.

Current-run diagnostics retain the bounded IDs of refreshed papers. Existing
normalized export includes their stable-key paper/source metadata, but not old
recommendations or historical summaries solely because an identifier changed.
The existing AWS alias guard can therefore accept the enrichment. A later routine
pull retains it instead of restoring the older missing DOI. No schema or protocol
section changes are required, and CORE's three-request/refill/cooldown limits remain.

This is future ingestion behavior, not repair of an existing conflicted database
or pending result batch. Existing durable outboxes are transmitted unchanged by
the retry path. The separate recovery proposal requires explicit approval.
