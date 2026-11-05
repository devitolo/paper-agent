# Held identity-conflict deliveries and routine recovery

Identity-conflict results are preserved as Held / Needs review. They are never
acknowledged as applied or automatically replayed. Source publication and the
lower-level delivery function reject sends while recovery is required.

## Routine sync only

Following the operator's November 4 direction, identity recovery uses the existing
`/sync/routine` download and `pull_routine()` merge. It does not call `/sync/backup`,
replace the working database, import full history from AWS, or raise backup limits.
The legacy `identity-restore-required.json` filename is retained for compatibility
with already-held batches; it now requires a routine refresh after explicit local
identity reconciliation. A genuine unresolved identity conflict remains an error;
the source-alias validation guard is unchanged.

The held capsule retains exact payload bytes/hash, post-run database, cycle marker
when present, referenced artifact bytes, and a manifest. Failure to preserve evidence
stops recovery. The acknowledged sequence remains separate from the reservation:
held sequence 33 is not success; later publications allocate at least 34. Future
publication of corrected held results requires explicit reconciliation and a fresh
reviewed identity/sequence, never automatic replay of the original payload.

## Explicit repair of the existing CORE duplicate

`paper_agents.core_identity_repair` is a one-batch operator command, never invoked
by cron or automatic sync. It is restricted to held batch
`176efddd00774894a8f36e9ba30141be`, capsule
`0cd8aa34a1522279d7b6bb3010d16abe2b1a0ca1d0be9ecab8d2d9b088f44f76`,
accepted sequence 32 and reserved sequence 33, with no active/pending work.

The reviewed rows share title and CORE source `313009506`. Established paper 991
has canonical key `core:313009506`; duplicate 1783 has DOI
`10.5281/zenodo.18302941`. The repair validates titles, DOI/arXiv and source ownership,
then plans against a disposable consistent SQLite copy. In apply mode it saves a
separate consistent pre-repair backup, verifies held artifact hashes/live bytes,
and updates the existing database transactionally under existing job/sync leases.

All declared foreign-key references move from 1783 to 991; evaluations,
recommendations, Scout history, feedback and artifact IDs are retained. If two
artifact registrations share the same path, the duplicate registration points to
its verified identical copy already in the held capsule. Neither artifact record
nor file is discarded. The redundant paper row is removed only after remapping;
its original complete row remains in the held database, pre-repair backup and
repair audit. Any unexpected identity, constraint conflict, row loss or FK/integrity
failure rolls back the transaction. The database inode is not replaced.

Dry run is the default:

```sh
python -m paper_agents.core_identity_repair \
  --db-path /app/data/paper_agent.db --state-dir /app/data/sync-state
```

Use `--apply` only for the explicitly authorized repair. It retains the recovery
marker; the subsequent normal routine merge clears it only on success. The repair
never publishes results, starts discovery, changes cursors/configuration/secrets,
or advances acknowledged/reserved producer sequences.
