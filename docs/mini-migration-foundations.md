# Mini migration foundations: first slice, not deployment clearance

> Historical slice record. Current first-cutover recipe: [mini-migration-candidate.md](mini-migration-candidate.md). Gemini startup behavior and the parked scheduler status below are superseded by that recipe.

This isolated change starts at `b4b3fc6` (application code identical to deployed
`6de3685`). It does not change ranking or manual Run Scout's arXiv-only behavior.
No native data, service, cron, model cache or Phoenix network is modified.

## Image foundations

The default Dockerfile final target `runtime` retains existing fresh-install
behavior and dependencies. The opt-in `migration` target additionally installs
curl, CA certificates, bash, util-linux (flock), coreutils (date), tzdata and the
existing optional OpenTelemetry requirements (SDK/common exporter 1.37.0).
The build runs `scripts/image_runtime_inventory.py`: command presence/version,
America/Los_Angeles zoneinfo and required telemetry imports are checked, and
resolved Python/package/tool versions are recorded at
`/app/image-runtime-inputs.json`. This is input evidence, not CPU/runtime clearance.

`PYTHON_BASE_IMAGE` is a build argument, defaulting to the existing
`python:3.12-slim` reference. No verified base or app digest is available in this
slice, so none is invented. Release qualification must select a real base digest,
record it together with resolved package versions, full source revision and
resulting image digest, and review transitive dependencies. Top-level telemetry
pins do not imply all transitive packages are locked or byte-reproducible builds.
No Docker build, package installation or image pull has been performed here.
Existing OCI revision/version/build-date arguments must be supplied by release
packaging; their default `unknown`/`dev` values are not production provenance.

## Explicit migration settings

`deploy/mini-migration.env.example` is an incomplete configuration contract, not a
Compose override or runnable migration recipe. Production secrets do not belong
in it. The current base Compose still has fresh-install settings; a reviewed Mini
overlay, mounts and network attachment are future work.

`PAPER_AGENT_STARTUP_MODE` defaults to `fresh`. `imported` requires packaged mode,
an explicit import manifest, a unique comma-separated allowlist of topic-form
default sources, explicit 0/1 Gemini and telemetry choices, verify-only model
mode, and the inventoried Qwen name/full digest. Invalid values fail before
initialization. Native and fresh-package source/Gemini defaults are unchanged.
Imported topics themselves are never rewritten.

Gemini=1 currently fails startup explicitly: Node/Gemini CLI/auth packaging has
not been implemented or qualified. Gemini=0 is an explicit operator choice, not a
migration default or parity claim. Existing successful native Gemini use must be
preserved in the later slice; do not choose zero merely to bypass the gate.
Telemetry=1 requires exact installed 1.37.0 components, importable SDK/encoder,
and an explicit validated OTLP endpoint. Initialization makes no exporter request.
Collector outages during normal use retain existing fail-open behavior. Container
network routing and actual export/trace visibility remain pending.

## Imported-state validation and compatibility

`paper_agents.import_state.manifest(root)` produces a read-only manifest of an
already isolated, quiesced snapshot. An operator must supply the approved manifest
from outside `data/` and `config/`, mounted read-only for startup. Do not generate a
new manifest from damaged destination files to make them pass validation. Snapshot
and transfer commands, permissions repair and production data collection are not
implemented in this slice.

Required inputs are existing `data/paper_agent.db`, `data/profile.json` and
`config/topics.yaml`. All regular files beneath data/config are hashed. Symlinks,
special files and SQLite journal/WAL/SHM sidecars are rejected: supply a consistent
standalone snapshot rather than a live directory. SQLite opens read-only with
immutable semantics, which relies on the quiescence contract. Parent-directory
aliases must also be controlled by the eventual transfer procedure; this code is
not a secure sandbox for hostile concurrent filesystem mutation.

Validation covers profile/topics structure, SQLite integrity and foreign keys,
exact schema equality against this image's `sql/schema.sql`, every table's full
row hashes and counts, and every artifact's resolving relative/internal data path.
Row hashes include IDs, fractional scores, historical and active profile/guidance,
raw/structured feedback, applied/pending relationships and diagnostics. No active
profile is inferred from the largest version number. Profile JSON and DB profile
versions are preserved independently; divergence is not silently reconciled.
Absolute/external artifact paths require a separate reviewed mapping and fail here.

**No schema deltas are approved in this slice.** Imported startup bypasses seeding
and `db.init_db`; an older/different schema is rejected instead of opportunistically
running the existing score-column table-rebuild migration. Exact stored schema SQL
is deliberately conservative: a semantically equivalent schema with different SQL
text can be rejected. Real snapshot compatibility must be checked before claiming
readiness; extend compatibility only through a reviewed explicit change.

The first startup verifies original file and logical hashes before creating any
initialization metadata. It takes the shared `data/.initialize.lock` nonblocking,
revalidates under the lock, then atomically publishes `data/import-accepted.json`
bound to the approved manifest. The lock and receipt are the only persistent successful import
startup additions. Staging uses exactly
`data/.import-receipt-<approved-manifest-sha256>.pending`. Before creating it,
the initializer durably writes that manifest's receipt payload into its locked
initialization file. Lock acquisition first rejects existing nonregular or
multiply linked paths, then opens without following symlinks and with nonblocking
semantics (exclusive creation if absent). The actual descriptor must be a regular
single-link file matching the intended path's device/inode, checked before and
after flock and again immediately before ownership writes. Hardlink aliases and
special files are rejected without deleting/replacing them or touching their
targets. Recovery happens only under the same lock, requires that
ownership payload and a regular staging file containing a prefix of the expected
receipt (including an empty/partial write), and revalidates the snapshot or already
published receipt before removing the reserved staging file. A second hardlink is
allowed only when it is the validated published receipt itself.

Missing/foreign ownership, malformed staging, symlinks, unexpected hardlinks,
reserved paths included in the input manifest, and unrelated temporary files are
rejected and preserved. No wildcard cleanup or ignore rule exists. Anonymous
orphans created by the earlier implementation are not automatically deleted.

Publication fsyncs staging contents before atomic no-clobber linking, syncs the
data directory after linking, removes staging, and fsyncs the receipt/directory
before success. Failures propagate; a later attempt retries durability even if
the receipt already exists. Filesystem fsync guarantees remain platform-dependent;
this is not a claim about storage hardware surviving arbitrary power loss. A
write/fsync exception or abrupt death before/after linking is recoverable. No DB/profile/topic
mutation occurs. Only one initializer can be active; a concurrent attempt fails
and must be retried explicitly.

On later starts/recreation, required files, parsing, integrity, relationships,
artifact resolution, schema and receipt identity are still checked. Original row
and file hashes are not enforced after acceptance because legitimate feedback,
profile and artifact updates must survive. The receipt is local operational state,
not a cryptographic authenticity proof. Keep it with its matching state/manifest;
never copy it into an unvalidated destination to skip the initial gate.

The future scheduler must depend on successful initialization, use the same data
volume and honor the existing pipeline `scout.lock`; this first slice does not
implement or start a scheduler. Imported startup requires quiesced writers,
including during recreation validation. The initialization lock alone cannot stop
native processes or future jobs that ignore it. Normal web startup still runs its
existing idempotent DB initialization after this gate; tests verify no changes on
the accepted exact schema. Native rollback and after-new-writes reverse transfer
still require a separately rehearsed procedure.

## Verify-only model identity

`PAPER_MODEL_MODE=verify` changes the existing prepare-model CLI to one bounded
GET `/api/tags`; it makes no pull, inference or retry. It requires one exact name
match and the full lowercase 64-character digest, rejecting short IDs, mismatches,
duplicates, malformed responses and missing models. An optional returned `model`
field must match `name`. The read is guarded by the existing POSIX total wall timer
(default 30 seconds, maximum 120). Imported preparation refuses default pull mode.
Fresh-install preparation continues to use its existing pull behavior.

Migration config pins `qwen2.5:1.5b-instruct` to
`65ec06548149b04c096a120e4a6da9d4017ea809c91734ea5631e89f96ddc57b`.
Imported app model readiness checks this identity before any readiness inference.
Identity checks report `inference: unchecked`; they do not load the model, validate
all blob bytes independently, establish CPU compatibility, or prevent another
actor changing a model tag after verification. Exclusive model-cache ownership and
actual Mini runtime checks remain necessary.

The response contract is based on the official [Ollama list-models API](https://docs.ollama.com/api/tags)
and the inventoried version's [v0.32.4 route implementation](https://github.com/ollama/ollama/blob/v0.32.4/server/routes.go).
Neither a model name nor its short CLI ID substitutes for the full API digest.

## Validation and pending gates

Synthetic tests cover missing/corrupt required inputs before mutations; altered
files/rows/schema/relations; missing/external/symlink artifacts; two startups and
recreation-equivalent copied directories; source originals unchanged; applied and
pending feedback; new writes surviving restart; cross-process initializer exclusion;
interrupted receipt publication; existing fresh defaults; invalid explicit config;
and one-read/no-pull exact model verification. Existing package tests continue to
exercise fresh-install preparation with mocked transports.

Pending: actual image build and dependency validation;
AVX-only Mini image/Ollama/model qualification and resource limits; real copied
snapshot compatibility/ownership; live scheduler and source/manual exclusion qualification;
Gemini runtime/auth; Phoenix network/export; full-state import/restore and after-write
reversal; reproducible release/publish/update procedure; and production cutover.
No first-slice test clears these gates.


## Bounded independent clearance — 2026-09-21

Architecture reported independent QA CLEAR for this first-slice implementation:
22 focused foundation tests passed. Independent reproduction confirmed the prior
PDF-hardlink lock alias is rejected with alias and bytes preserved and no receipt,
and that the prior fsync-failure case recovers on retry. Architecture accepted this
bounded local slice after those fixes. This is evidence for the uncommitted
`codex/mini-migration-foundations` worktree based on `b4b3fc6`, not a published image
or an approval to migrate production.

The separate scheduler/Compose slice also received bounded independent clearance
on the same date; see [its evidence record](mini-migration-scheduler.md#bounded-independent-clearance--2026-09-21).
Neither clearance establishes image build compatibility, real imported-state
compatibility, live execution, or cutover readiness. The pending gates above remain.
