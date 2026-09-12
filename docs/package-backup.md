# Packaged backup and restore

The helper archives all files in the installation's `paper-data` and `paper-config` volumes: SQLite history/feedback, profile, topics, downloaded artifacts, logs and runtime metadata. It checks SQLite integrity, required state files and the actual compressed archive before reporting success. Restoration requires empty destination volumes and the identical immutable application image digest. It is a same-release recovery procedure, not an upgrade or rollback mechanism.

The Ollama model cache is excluded; the installer can download it again. Keep a private copy of `.env`, `.paper-install` and the matching repository checkout/helper alongside each archive. These host files are not included automatically. The archive manifest records the app image digest. A backup contains sensitive research and feedback; it is private by file permissions, **not encrypted**. Use an encrypted independent storage destination and never attach it to a public issue.

## Create a backup

Run from your installed repository checkout. Wait for discovery to finish. Use the project name in `.paper-install`, not the example placeholder:

```bash
cat .paper-install
docker compose -p paper-YOUR-PROJECT stop app
mkdir -p "$HOME/paper-backups"
chmod 700 "$HOME/paper-backups"
bash scripts/package_backup.sh backup "$HOME/paper-backups/paper-2026-09-12.tar.gz"
docker compose -p paper-YOUR-PROJECT up -d app ollama
```

Choose a new filename each time. The command refuses existing archives, active containers using the volumes and concurrent installer/backup operations. If backup fails, your original state remains in place; restart the app with the last command and correct the reported problem before retrying. No host Python is required.

Copy the archive and private configuration to independent storage. Keep seven daily backups and four weekly backups. Scheduling and pruning are manual in this release: only remove older copies after a newer verified archive exists, retain the newest known-good copy, and periodically rehearse restoration. A backup on the same disk does not cover disk loss.

## Restore into a separate installation

1. Obtain the matching repository checkout containing this helper in a **new directory**. Do not run the installer yet: it would initialize the destination volumes.
2. Copy your saved `.env` into that directory, retaining the original immutable app and Ollama image selections. Keep the original `.paper-install` as reference, but **do not copy it into the new checkout**. If the original app is still running, choose an unused `PAPER_PORT` in the copied `.env`.
3. Pick a unique Compose identity beginning with `paper-`, then run:

   ```bash
   PAPER_RESTORE_PROJECT=paper-recovered-20260912 bash scripts/package_backup.sh restore /absolute/path/paper-2026-09-12.tar.gz
   bash scripts/install_project_paper.sh
   ```

4. Open the URL printed by the installer. Verify topics, saved feedback, papers and profile before adopting this installation. Keep the original installation and archive until this check succeeds.

The helper validates the archive before copying state, restores app ownership, and writes the new installation record. It refuses populated destination volumes and mismatched images; it never replaces an existing installation's data. If copying is interrupted or the destination runs out of disk, keep the archive and retry with a new empty project/directory after fixing capacity. Do not delete the original installation to repair a failed restore.

Allow space for the compressed archive, its uncompressed verification staging inside Docker, and the restored volumes. Downloading images/model weights requires network access when not cached. Discovery interrupted at backup time is not resumed from mid-execution; inspect Runtime and retry after readiness.

## Verification boundary

See [backup drill evidence](package-backup-qa.md). The same-image archive round trip is separate from fresh-install acceptance. Cross-version migration, rollback, automatic retention, disaster recovery without the saved configuration, and a native Mini restore are not covered by this helper.
