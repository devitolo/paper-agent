# Consolidated observability recovery repair — 2026-10-25

## Problem and change

The earlier installer could lose the old recovery pointer on failure; rollback could consume or overlook recovery data, damage restored files on retry, or follow collector-controlled receipt paths. The repair treats installation and rollback as one recoverable transaction rather than independent file operations.

The candidate is based on7179170 and currently lives in `/tmp/paper-observability-recovery-complete`. Its exact files are identified by the final gate's hashes; a commit SHA alone does not identify this uncommitted candidate. The developer reviewed it and added all-target symlink rejection before any restoration mutation.

- A shared host-only `recovery-common.sh` validates all four unique managed records, original existence, exact backup paths, checksums, UID/GID/mode and regular-file trust boundaries before recovery changes anything.
- Recovery copies and manifest remain immutable. Each restored file is prepared separately and replaced atomically; receipt preparation happens under root-owned control, with same-filesystem validation before rename into the collector-writable export.
- `PENDING-BACKUP` records the prepared transaction before application files change. If installation and automatic restoration both fail, normal removal resumes that pending recovery and preserves the previous CURRENT-BACKUP transaction.
- CURRENT-BACKUP is prepared with correct metadata before atomic replacement. Completion is recorded separately from the manifest, so interrupted restoration and final pointer cleanup can be repeated.
- Completed pending recovery has its own root-owned marker, so a repeated recovery command preserves the restored installation rather than rolling back an older transaction. The next successful install clears that marker.
- The release archive includes the new helper. Its real tar command was exercised locally, and the helper's inclusion was verified.

No ranking logic, database schema, provider configuration, key2, production host or deployed service was changed.

## Test strategy and implementation

`tests/test_observability_installer_linux_qa.py` covers25 Linux methods including the intentional45-second client-timeout probe. `tests/test_observability_recovery_matrix_qa.py` adds nine methods with per-file/subscenario coverage: fresh/upgrade publication failures, each restoration failure, atomic pointer failure, failed automatic recovery, corruption, malformed/incomplete/duplicate manifests, metadata/path errors, backup and export symlinks, and preservation of unrelated samples/exports. Tests assert real fault markers so obsolete injection points cannot create misleading results.

`qa/run_observability_recovery.py` freezes whitelisted candidate files and independent tests, requires an existing local AMD64 image and Unix-socket Docker context, runs in disposable network-disabled Linux with only source mounted read-only, rejects skips, bounds execution, cleans its named container and saves logs/input hashes. The existing image is only a Linux/Python/Bash test harness; no Docker socket or host configuration is mounted.

## Results

**Final reviewed candidate:52/52 methods pass; zero failures, errors or skips.** The final run includes the developer-reviewed preflight and repeated pending-recovery safeguard. Earlier complete repair runs also passed52/52; only the latest frozen source is the approval candidate. The7179170 baseline passed47/51 methods and failed four method scenarios; the final suite also includes the executable, already-agreed double-failure recovery requirement.

Commands:

```sh
python3 qa/run_observability_recovery.py --source /tmp/paper-observability-recovery-complete --image paper-observability-qa:e579dca --output /tmp/paper-observability-complete-gate-final
```

The52 methods comprise25 base Linux installer methods, nine independent matrix methods and18 established collector/package/release tests. Actual workflow tar packaging and shell syntax were also checked. Full image runtime is unchanged by this host-only repair; prior AMD64 collector image runtime evidence remains separate.

Final evidence is retained under ignored `qa/results/observability-consolidated/final/`; it contains exact source hashes, image ID, per-test output and machine-readable results. `qa/patches/observability-recovery-complete.patch` contains the scoped source, test, runner and documentation changes against7179170. The patch passes `git apply --check --whitespace=error-all`.

## Scope and limits

The injected command/file failures, repeats, content preservation, file metadata and collector/root trust boundaries are tested. This is not power-loss/fsync or full SIGKILL certification, native AMD64 host certification, real daemon-container timeout cleanup, or a full live cron → Docker → host export → Health workflow. Production was not accessed, no release was dispatched and no image was published.

Other previously reported metric/Health defects remain separate: corrupt CPU baselines, reader schema/future/sensitive-field validation and the collector/Health disk contract. Passing this recovery gate does not close those issues or approve deployment of the whole feature.

The complete repair and scoped tests are prepared for review together. No further partial patches should be represented as this full acceptance gate.
