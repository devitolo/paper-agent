# Existing CORE conflict recovery — proposal, not executed

Known Nov 1 case: AWS paper 991 already owns the CORE source alias with a core-based
canonical key and no DOI. Mini produced a DOI-based paper 1783 and pending batch
176efddd00774894a8f36e9ba30141be, sequence 33. SRE established that AWS rejected
the alias identity conflict; the revision exists and the batch is not applied.
The original pending bytes and both records' histories must remain recoverable.

The future ingestion fix does not rewrite this batch or repair these existing
rows. No production repair, quarantine, retry, provider call or rekey is authorized
by the local code task. A separate, explicitly approved recovery should:

1. Under the existing publication/job leases, take consistent Mini and AWS
   backups and immutable copies/hashes of the exact pending batch, sequence,
   recovery marker and affected paper/history graphs. Recheck batch absence and
   accepted producer sequence immediately before any eventual publication.
2. Stage a reconciliation in disposable copies. Verify exact source ID ownership,
   normalized DOI and bibliographic evidence. Stop if identifiers point to genuinely
   different papers. Retain AWS's established canonical identity and alias owner;
   do not weaken its guard or overwrite source ownership to force acceptance.
3. Produce a reviewed mapping for the two Mini records and all affected Scout,
   evaluation, recommendation, review, feedback and artifact references. Preserve
   row history and original audit snapshots; preflight every uniqueness/FK conflict.
   Do not silently drop rows or blindly replay a duplicate recommendation. A
   metadata-only correction may be appropriate for an already-known paper, but
   the intended historical/result disposition must be reviewed explicitly.
4. Build and validate a proposed corrected delta against an AWS copy. Preserve
   original processing observations and the immutable original batch. Use a fresh
   batch identity and valid sequence with an explicit supersession audit record;
   never reuse an idempotency key for changed bytes. Show the exact staged DB and
   payload diff for approval before altering live state or pending retry slots.
5. Only after that approval, apply the exact transactional Mini repair, publish
   once through the normal signed path, verify the applied AWS receipt and Health
   continuity, and compare data/history/configuration/cursor invariants. Retain
   backups and original pending bytes for rollback. Do not rerun discovery to
   manufacture replacement evidence.

No commands or automatic repair mechanism are installed by this proposal. Until
approved reconciliation, the preserved pending batch can continue blocking later
source retries/pulls; a code deployment alone must not be represented as recovery.
