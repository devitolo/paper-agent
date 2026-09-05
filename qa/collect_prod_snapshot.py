#!/usr/bin/env python3
"""Collect read-only production diagnostics for offline ranking QA."""
from __future__ import annotations

import argparse
from collections import deque
from contextlib import closing
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tarfile
import tempfile
import time


def command_output(command: list[str], repo: Path) -> str:
    try:
        result = subprocess.run(command, cwd=repo, capture_output=True,
                                text=True, timeout=15, check=False)
        return f"exit_code: {result.returncode}\n{result.stdout}{result.stderr}"
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"unavailable: {error}\n"


def collect(repo: Path, source: Path, output: Path) -> Path:
    if not source.is_file():
        raise ValueError(f"Database not found: {source}")
    os.umask(0o077)
    output.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="paper-agent-qa-", dir=output))
    snapshot = folder / "paper-agent-qa.db"
    print("Creating consistent SQLite snapshot...", flush=True)
    deadline = time.monotonic() + 120

    def progress(status: int, remaining: int, total: int) -> None:
        if time.monotonic() > deadline:
            raise TimeoutError("Database backup exceeded 120 seconds; retry when less busy.")

    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as reader:
        with closing(sqlite3.connect(snapshot)) as writer:
            reader.backup(writer, pages=256, progress=progress, sleep=0.1)
            integrity = [row[0] for row in writer.execute("PRAGMA integrity_check")]
    if integrity != ["ok"]:
        raise ValueError(f"Snapshot integrity failed: {integrity}")
    (folder / "integrity.txt").write_text("ok\n")
    print("Collecting commit, schedule, and recent logs...", flush=True)
    for name, command in {
        "commit.txt": ["git", "rev-parse", "HEAD"],
        "git-status.txt": ["git", "status", "--short"],
        "crontab.txt": ["crontab", "-l"],
    }.items():
        (folder / name).write_text(command_output(command, repo))
    for name in ("pipeline-daily.log", "pipeline-openalex.log", "pipeline-semantic-scholar.log"):
        try:
            with (repo / "logs" / name).open(errors="replace") as log:
                content = "".join(deque(log, maxlen=200))
        except OSError as error:
            content = f"unavailable: {error}\n"
        (folder / name).write_text(content)
    (folder / "manifest.json").write_text(json.dumps({
        "captured_at": datetime.now().astimezone().isoformat(),
        "source_db": str(source.resolve()),
        "integrity": "ok",
        "log_lines_per_file": 200,
        "note": "Contains saved feedback. Inspect logs and cron for credentials before sharing.",
    }, indent=2) + "\n")
    archive = folder.with_suffix(".tar.gz")
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(folder, arcname=folder.name)
    return archive


def main() -> None:
    repo = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=repo / "data/paper_agent.db")
    parser.add_argument("--output-dir", type=Path, default=repo / "qa/bundles")
    args = parser.parse_args()
    try:
        archive = collect(repo, args.db, args.output_dir)
    except (OSError, sqlite3.Error, ValueError, TimeoutError) as error:
        parser.exit(1, f"Collection failed: {error}\nNo completed bundle was produced.\n")
    print(f"Snapshot integrity: ok\nQA bundle: {archive}")
    print("Contains saved feedback; inspect logs and cron for credentials before sharing.")


if __name__ == "__main__":
    main()
