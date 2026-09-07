#!/usr/bin/env bash
set -euo pipefail

LOCK_DIR="${PAPER_AGENT_LOCK_DIR:-/tmp}"
LOCK_PATH="$LOCK_DIR/project-paper-backup.lock"
mkdir -p "$LOCK_DIR"
exec 9>"$LOCK_PATH"
if ! flock -n 9; then
  echo "Skipping database backup: another run holds $LOCK_PATH."
  exit 0
fi

SOURCE_DB="${1:-data/paper_agent.db}"
BACKUP_DIR="${2:-backups}"

python3 - "$SOURCE_DB" "$BACKUP_DIR" <<'PY'
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

source = Path(sys.argv[1])
backup_dir = Path(sys.argv[2])

if not source.exists():
    raise SystemExit(f"source database does not exist: {source}")

backup_dir.mkdir(parents=True, exist_ok=True)
timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
destination = backup_dir / f"paper_agent-{timestamp}.db"

try:
    with sqlite3.connect(source) as source_connection:
        with sqlite3.connect(destination) as backup_connection:
            source_connection.backup(backup_connection)
            result = backup_connection.execute("PRAGMA integrity_check").fetchone()
except sqlite3.Error as error:
    if destination.exists():
        destination.unlink()
    raise SystemExit(f"backup failed: {error}") from error

if not result or result[0] != "ok":
    if destination.exists():
        destination.unlink()
    detail = result[0] if result else "no integrity_check result"
    raise SystemExit(f"backup integrity_check failed: {detail}")

print(f"backup ok: {destination}")
PY
