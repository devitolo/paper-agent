# Observability recovery acceptance gate

This is the consolidated acceptance contract agreed for the rollback repair. A final candidate must satisfy the complete matrix; individual fixes or successful static tests do not constitute approval. Tests use disposable local Linux containers and synthetic files only. Production, deployment and key2 are excluded.

## Required transaction properties

1. Before mutation: acquire the shared lock, validate complete safe image input, validate the root-owned control boundary, capture original existence/content/metadata and prepare immutable recovery data.
2. Before recovery mutation: validate the complete manifest, exact managed target set and all required backup existence/integrity. Distinguish original absence from missing recovery data. Invalid recovery evidence must preserve the current installation and return a failure.
3. Publish prepared managed files atomically. Publish the new pointer atomically only when it refers to durable validated recovery data. Any failed upgrade retains the previous installation and previous recovery pointer, or leaves explicit retryable recovery evidence if restoration itself fails.
4. Restore without consuming recovery copies. Repeat/interruption must not delete already restored data. File contents and original mode/owner must survive recovery.
5. Persist completion before clearing the active pointer. A completed restore with incomplete cleanup must be distinguishable from missing/corrupt active evidence and finish safely on retry. Repeated successful rollback preserves the restored installation.
6. Collector UID10001 cannot modify control metadata/backups or cause rollback outside the managed file set. Existing samples, exports and unrelated files are preserved.

## Fault matrix

| Boundary | Expected verification |
| --- | --- |
| Invalid image / held lock | Failure before managed file/directory mutation; metadata unchanged |
| Fresh install / upgrade | Correct bytes/mode/owner; useful receipt; validated durable recovery state |
| Failure replacing each launcher/config/cron/receipt | Previous complete managed state and prior pointer preserved, or explicit recoverable failure |
| Pointer metadata preparation / publication failure | Previous pointer remains valid; no dangling new pointer |
| Install error recovery itself fails | Recovery evidence retained; a later retry can recover; no backup consumption |
| Active manifest missing, malformed or unknown targets | Nonzero, current files unchanged, clear diagnostic |
| Required backup missing or content-corrupt | Nonzero before any target deletion; original absence is separately represented |
| Each file restoration fails once | Retry restores complete old contents and metadata without lost backups |
| Restore completion-marker publication fails | Retry safely resumes or repeats restore |
| Pointer removal fails after completion | Retry completes cleanup and preserves the restored installation |
| Repeated rollback | Both attempts succeed and preserve the same restored state |
| Collector metadata tamper / forged state manifest | Write denied or ignored; unrelated root fixture unchanged |
| Unrelated samples/exports/files | Contents preserved through install, failed upgrade and rollback |

Existing independent tests are in `tests/test_observability_installer_linux_qa.py`. Fault hooks must intercept the actual operation (copy/replace/etc.) and assert that the injected fault fired. An obsolete injection that no longer runs must not be mistaken for a source failure or a pass.

## Final gate and reporting

Run the complete installer matrix against one frozen candidate with exact source/test hashes. Include existing package/release checks and actual AMD64 image runtime regression where changed. Report separately: functional outcomes, database/state preservation, permissions/trust boundary, fault/retry outcomes, runtime/image checks and unverified boundaries. Correct real defects in the same candidate; do not weaken assertions to obtain a pass.

Remaining metric/Health issues are a separate outstanding gate: corrupt CPU baseline recovery; malformed/future/sensitive export validation; producer/consumer disk schema. A rollback pass does not close them.

Actual daemon-owned container cleanup, full installed Linux cron → real Docker → host export → Health integration and native host measurements require their own evidence. Command-failure injection is not SIGKILL/power-loss certification; durability claims beyond it must be tested or explicitly bounded. No claim of eliminating every future failure is made.

QA prepares scoped tests/evidence and the developer prepares a complete repair for review. Commit/push remain ready for human approval; production stays untouched.
