"""Small durable audit log for scheduled job starts and skips."""
from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
from typing import Any


def event_path() -> Path:
    return Path(os.environ.get("PAPER_AGENT_JOB_EVENT_FILE", "/app/data/job-events.jsonl"))


def record(job: str, status: str, message: str, *, path: Path | None = None) -> dict[str, Any]:
    target = path or event_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "job": job,
        "status": status,
        "message": message,
    }
    line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
    descriptor = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        os.write(descriptor, line.encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return event


def recent(path: Path, *, limit: int = 30) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except OSError:
        return []
    events = []
    for line in lines[-max(1, limit):]:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(event, dict)
            and isinstance(event.get("recorded_at"), str)
            and isinstance(event.get("job"), str)
            and event.get("status") in {"started", "completed", "failed", "skipped_busy"}
            and isinstance(event.get("message"), str)
        ):
            events.append(event)
    return list(reversed(events))
