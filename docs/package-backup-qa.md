# Packaged backup/restore evidence — 2026-09-12

Result: same-image Docker backup/restore passed on macOS Apple Silicon using Linux arm64 containers, Docker Engine 29.6.2 and Compose 5.3.1.

Application image:
`ghcr.io/devitolo/paper-agent@sha256:116e994bef7294767df08754c20870c596946a916e4e66f047efc973c858df13` (v0.1.3).

The helper executes from the repository using Python in that existing image; no application image rebuild or ranking change is involved.

## Checks performed

- Python unit round trip preserved synthetic SQLite feedback, profile, topic configuration and artifact bytes.
- Unit checks rejected a mismatched image, populated destination, truncated archive, path traversal and an existing backup filename. Existing archive bytes were unchanged.
- Docker drill initialized a real v0.1.3 database in isolated volumes, added synthetic state to both volumes, backed up, and restored into a separate project.
- Restored files were readable by app UID 10001; normal application initialization succeeded and the web readiness check passed.
- The helper wrote a fresh installer identity with the matching app image.
- Backup while the restored app was running was refused. Reusing an archive filename was refused without modifying the archive.
- Temporary test containers and volumes were removed. No customer volumes or native Mini services were changed.

The initial drill passed restoration but hit an occupied port during web startup. The repeat used an automatically allocated loopback port and passed. Evidence directory from the passing run on the test host: `/private/tmp/paper-backup-smoke-0nj5j05j` (temporary, not a distributed artifact).

Reproduce:

```bash
python3 -m unittest tests.test_package_backup
bash -n scripts/package_backup.sh
python3 tests/package_backup_docker.py
```

The Docker drill requires host Python only as test tooling; customer backup/restore does not. It allocates and removes its own test projects and leaves synthetic archives in the system temporary directory.

## Ubuntu x86-64 confirmation

The same-image backup/restore gate also **passed on Ubuntu 24.04 x86-64** in [GitHub Actions run 34721084164](https://github.com/devitolo/paper-agent/actions/runs/34721084164), at commit `020396027c50347f703681472f5efb7c2499992b`. The job ran from 2026-09-12 21:50:30 to 21:50:57 UTC.

GitHub reports success for environment recording, unit safety checks, the published-image Docker drill, and evidence upload. The drill additionally seeds and verifies a synthetic saved value in SQLite. No arXiv requests, model download, customer state or native Mini operations are needed.

Artifact: `project-paper-linux-backup-restore-34721084164` (14-day retention). It contains the runner/Docker/Python versions, selected image and source commit, structured result, drill output and synthetic app logs. Archives and customer configuration are not uploaded.

To repeat, open **Actions → Backup Restore → Run workflow**. Changes to the helper, tests or workflow on `main` also run this gate automatically. The workflow uses the immutable app image selected in `.env.example`.

## Limits

This is a developer-run recovery drill, not independent tester sign-off. Cross-architecture restoration, full model re-download following restore, disk-full/interruption fault injection, and visual verification of restored review content remain untested here. Fresh-install model/discovery evidence is separate in [v0.1.3 acceptance](v0.1.3-acceptance.md).

Automatic retention/pruning and cross-version upgrade/rollback are deferred. The runbook specifies manual seven-daily/four-weekly retention and independent storage. The helper refuses populated targets rather than replacing existing state; a failed partial copy requires another empty target.
