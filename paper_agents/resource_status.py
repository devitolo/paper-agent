"""Read-only, sanitized resource status model shared by CLI and Health UI."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any


SCHEMA = "project-paper.resource-status.v1"


def load_resource_status(path: Path | None = None, *, now: datetime | None = None) -> dict[str, Any]:
    target = path or Path(os.environ.get("PAPER_AGENT_RESOURCE_STATUS_FILE", "data/resource-status.json"))
    unknown: dict[str, Any] = {"schema": SCHEMA, "state": "unknown", "observed_at": None, "age_seconds": None, "sample_count": 0, "coverage_seconds": 0}
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {**unknown, "state": "unavailable"}
    if not isinstance(value, dict) or value.get("schema") != SCHEMA or not isinstance(value.get("observed_at"), str):
        return {**unknown, "state": "malformed"}
    current = now or datetime.now(timezone.utc)
    try:
        observed = datetime.fromisoformat(value["observed_at"].replace("Z", "+00:00"))
        age = max(0, int((current - observed).total_seconds()))
    except (TypeError, ValueError):
        return {**unknown, "state": "malformed"}
    state = str(value.get("state", "observed"))
    if age > 45 * 60:
        state = "stale"
    return {**value, "state": state, "age_seconds": age}
