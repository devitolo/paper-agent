# AWS-to-Mini full snapshot foundation (Task 1)

This is an offline, opt-in implementation based on repository `origin/main`
revision `ed242f8`. It adds no listener, network calls, runtime startup changes,
cron entries, Health UI, SSH commands, deployment, or provider/model execution.
The existing pipeline continues using its existing state until a separately
implemented consumer adopts this input store.

## Implemented boundary

`paper_agents.full_snapshot` exports a versioned package. `snapshot_store`
receives a package through a three-file stream interface, authenticates it,
validates it in staging, and atomically selects the complete input. The concrete
`LocalPackage` adapter reads a local package directory only. **A live Tailscale /
rsync adapter is not implemented or configured.** The private transport contract
and prerequisites below are required before a live transfer can be enabled.

The input is the complete approved application state, not the whole host or a
replacement for disaster-recovery backup. A receiver store contains immutable
input versions. Consumers must hold `selected_input(...)` for the complete
read/copy operation, and put subsequent mutable pipeline outputs elsewhere.
No existing Docker volume or live `data/` directory is overwritten. Selecting a
version does not remount a container or update existing open SQLite connections.
Task 2 must explicitly integrate materialization and the writer lifecycle.

## Allowlist and consistency

Required files:

| Path | Policy |
| --- | --- |
| `data/paper_agent.db` | SQLite online backup, standalone DELETE journal mode, VACUUM to remove free/deleted pages |
| `data/profile.json` | Profile business fields only; preserve independent JSON/DB profile history |
| `config/topics.yaml` | Parse and render approved topic fields; ignored preambles/comments are removed |
| `config/industry.json` | Approved Industry / ZenML configuration fields |
| DB-referenced non-PDF artifacts | `triage_summary` or `review`; `.json`, `.md`, `.txt` under `data/extractions`, `data/zenml-pilot`, or `data/papers` |

The current main schema stores OpenAlex, arXiv, Semantic Scholar and CORE
traversal state in the main database. There are **no additional approved DB
paths** in v1. The scheduler journal is runtime state and is excluded. Adding an
auxiliary pipeline DB requires a reviewed exact path/schema and coordinated
capture policy; the exporter never discovers and copies arbitrary `*.db` files.

All tables in `full_snapshot.TABLES` are retained, including profile/feedback
history and stored MiniLM experiment results. MiniLM/Ollama model weights are
excluded. Table names must match this explicit allowlist; schema SQL must match
the existing `import_state.trusted_schemas()` compatibility set. The exporter
does not run schema migrations. Unknown tables, columns outside those exact
schemas, credential-like column names, and credential-like keys in stored JSON
are rejected. Errors never include their values. Known configuration keys are
allowlisted. Free-text business data is preserved; this is not a claim to detect
arbitrary secrets pasted into notes, URLs, summaries, or prose. The source owner
must keep such secrets out of business records.

Only referenced non-PDF files are copied. PDF artifact rows remain in the DB,
and their IDs/relative paths are explicitly recorded as excluded references;
PDF bytes are never read or transferred. All other referenced artifacts must
exist and match the approved type/path. Absolute historical artifact paths need
a separately reviewed mapping; v1 fails closed rather than rewriting them.
Embedded JSON is business data, not an instruction to fetch more paths or URLs.

Everything else is excluded: `.env`, credentials, logs, locks, runtime receipts,
model caches, backups, nonprod/test DB files, Docker/host state, SQLite WAL/SHM/
journal sidecars, and unreferenced artifacts. Known nonprod provenance markers
cause source refusal. Paths must be canonical relative ASCII paths; symlinks,
hardlinks, special files and sensitive path names are rejected for included
files. Both roots and their ancestors must be real directories without symlink
aliases. The source/destination may not overlap.

**The caller must fence every source writer across the complete export**, not
just database writes: web feedback/config changes, scheduled/manual pipeline
runs and artifact writers. `--source-quiesced` acknowledges this prerequisite;
it does not stop processes or acquire their locks. SQLite opens the source in
read-only mode and uses the backup API to include committed WAL data. The
exporter detects DB `data_version` changes and checks source file hashes again
before publication, but these checks cannot replace a global writer fence.
Concurrent hostile filesystem mutation and uncooperative writers are outside
the contract. Run against a private controlled source, preferably through a
read-only mount after fencing. No live-source consistency clearance is implied.

## Package and authentication

Each immutable publication is `snapshot-<20-digit-monotonic-revision>/`:

```text
manifest.json
auth.json
snapshot.tar.gz
payload/data/...
payload/config/...
```

The exporter retains the standalone payload for inspection. The adapter only
serves the first three files. Publication renames a fully fsynced staging
directory on the same filesystem. Reusing a revision fails without overwriting
the old publication. Source revision allocation and single-exporter exclusion
are operator responsibilities.

The manifest records format/policy/stream identity, monotonic snapshot revision,
40-character source Git revision, UTC capture time, per-file sizes and SHA-256,
database schema SHA-256, SQLite user version, per-table row counts, exclusions,
and omitted PDF references. The archive is regular-file-only USTAR compressed
with `gzip` level 9 (`mtime=0`, no original filename). It includes only payload
files, without host ownership, xattrs, links or special entries.

`auth.json` binds the exact manifest bytes, compressed archive SHA-256 and byte
length using HMAC-SHA256 over canonical JSON with a protocol-specific prefix.
The key is at least 32 bytes and stored outside the export source/destination in
a mode-0600 file. Pass a key **file path**, never a secret command-line argument.
HMAC authenticates content; it does not encrypt it or authenticate a network
client. The private transport separately provides confidentiality and peer /
restricted-account authentication. Any HMAC key holder can forge a package.
Key rotation must retain the previous key until required rollback packages have
been retired, or define a reviewed re-signing/key-ID protocol; v1 uses one key.

Receiver validation occurs before selection: authentication precedes archive
decompression; then manifest identity, age (seven days, five-minute future clock
tolerance), schema, integrity/foreign keys, sizes/hashes, file allowlist and
artifact references are checked. The archive reader accepts only regular USTAR
entries and verifies complete gzip/trailer consumption. It rejects duplicate
entries, traversal, links, PAX/GNU extension records, case-colliding file paths,
extra/missing files, malformed JSON and duplicate JSON keys.

Limits are 20,000 files, 1 GiB of payload, 512 MiB per file/DB, 4 MiB per config,
JSON artifact or DB JSON field, 8 MiB manifest, 4 KiB auth, and 1 GiB + 32 MiB
compressed archive. SQLite backup has a 120-second deadline. The future remote
adapter must additionally enforce connection, idle and total transfer deadlines.
Do not silently raise limits for real data; qualify any changed limit explicitly.

## Mini selection, rollback and interruptions

Use a dedicated mode-0700 store outside all source/export/live state roots:

```text
input-store/
  .snapshot.lock
  state.json
  versions/snapshot-<revision>-<unique-id>/
    manifest.json
    auth.json
    snapshot.tar.gz
    transfer-owner.json
    payload/...
```

One exclusive lifecycle lease protects receive/install/rollback. Readers hold
the matching shared lease through `selected_input`. The atomic `state.json`
record contains current input, previous input, stream identity and revision/time
high-water marks. A durable validated version is published first, then all those
fields switch in **one** `os.replace` followed by directory fsync. Data and config
cannot be selected from different versions. Consumers resolve the current
version through this API, not by enumerating version directories.

An explicit rollback authenticates and validates the previous version and swaps
current/previous together. It permits an older capture timestamp but keeps the
highest accepted revision/time, so a duplicate or stale pull cannot reapply a
rolled-back revision. Both versions and all their bytes remain intact. Changed
or corrupt previous inputs block rollback. This is input rollback, not reversal
of subsequent pipeline outputs or a merge of new business writes. No pruning
is implemented; provision disk for compressed and expanded retained versions.

Ordinary transfer/validation failures clean their staging directory and preserve
the prior pointer. On the next pull, identifiable abandoned `.incoming-*`
directories are removed under the exclusive lease using their exact ownership
marker; unknown directories are preserved. A crash before marker publication
may leave an unowned empty directory for operator review. No wildcard deletion
of unrelated user state occurs.

If a process dies after version publication but before pointer commit, the old
pointer remains authoritative and an orphan version may remain. On first install,
a missing pointer alongside versions fails closed for explicit operator recovery
instead of silently resetting replay protection. With an existing pointer, a
retry can publish a new unique directory. A directory-fsync failure after atomic
replace can report failure while the new complete input is already selected;
inspect `state.json` under the lease before retrying. Never delete/rebuild it to
bypass a duplicate rejection. Restore the store and pointer together from a
trusted backup. Durability relies on the filesystem's fsync/rename guarantees.

## Offline invocation

These commands are examples for prepared isolated directories, not live setup
commands. Use canonical absolute paths (resolve macOS `/tmp` aliases first).

```bash
python3 -m paper_agents.snapshot_store export /isolated/aws-state /isolated/exports \
  --stream aws-production --revision 1 --source-revision <full-git-sha> \
  --key-file /private/snapshot.key --source-quiesced

python3 -m paper_agents.snapshot_store pull-local \
  /isolated/received/snapshot-00000000000000000001 /isolated/mini-input \
  --stream aws-production --key-file /private/snapshot.key

python3 -m paper_agents.snapshot_store rollback /isolated/mini-input \
  --stream aws-production --key-file /private/snapshot.key
```

CLI output contains only operation, success/failure, and successful revision.
The CLI suppresses raw exception text and never logs payloads, secrets or paths.
Library `SnapshotError` messages provide fixed diagnostic categories.

## Private adapter prerequisites (not implemented)

Before any live transfer, provision and review:

1. A private AWS Tailscale peer and pinned identity/address; ACLs permitting only
   the Mini sync identity to reach the export service. No public bind, router
   forwarding, ingress rule, public DNS fallback or redirect.
2. A dedicated read-only restricted sync account/module, confined to the immutable
   publication directory and exactly the three package filenames. It must not
   expose the application root, shell, host filesystem or credentials. rsync
   must be configured for this restriction; no generic SSH adapter is assumed.
3. A producer writer fence and monotonic revision allocator, retention policy,
   disk allowance, matching schema/application revisions and synchronized clocks.
4. An out-of-band shared HMAC key and separate transport account credentials,
   restrictive ownership/modes and a rotation/rollback policy.
5. A `PackageSource.open(name)` implementation scoped to one fixed publication,
   initiated only by Mini. It must stream binary data, enforce deadlines and
   cancellation, reject public fallback, and avoid logging secrets. Receivers
   always perform independent HMAC/content validation; transport success alone
   does not select an input. A discovery/catalog protocol is also still needed.
6. A consumer integration that holds the store lease while copying an immutable
   input to a separately fenced mutable pipeline workspace; failure must skip
   execution. Qualify real volume mounts and source/destination permissions.

Only the offline adapter and protocol are tested here. No Tailscale reachability,
rsync authorization, live data schema/path compatibility, deployment, or cutover
is claimed. Existing imported-startup manifests are a separate protocol and
require all artifact bytes; do not pass these PDF-excluding packages to that
startup gate as though they were interchangeable.

## Validation

Run focused tests with:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests/full_snapshot -v
```

Fixtures cover WAL/uncommitted writes, source immutability and mutation refusal,
allowlists/secrets/PDF omissions, schema/integrity/foreign keys, manifest/gzip/HMAC,
archive bounds and hostile entries, replay/freshness, interrupted transfers,
owned-stage recovery, atomic selection failure and ambiguous fsync recovery,
reader exclusion, rollback retention, replay protection after rollback, lost
pointer refusal and CLI secret redaction. All fixtures are synthetic and offline.

Local evidence, 2026-10-20 (Python 3.14): 31 focused tests passed; compileall and
`git diff --cached --check` passed. Full discovery ran 600 tests, with 22 skipped
and one pre-existing error in
`GeminiProcessTests.test_cleanup_error_preserves_timeout_and_attempts_kill_after_term_error`:
the unchanged `gemini_process._cleanup` compares a mock attribute with a float.
That error reproduces independently, and both its implementation and test are
identical to the base revision. The initial sandbox run also blocked `ps` in
process-cleanup tests; the unrestricted rerun removed those permission errors.
The full suite is therefore not entirely green; no unrelated Gemini fix is
included in this Task 1 change.
