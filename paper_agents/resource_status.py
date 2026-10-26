"""Read-only, sanitized resource status model shared by CLI and Health UI."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any
import math


SCHEMA = "project-paper.resource-status.v1"
FUTURE_SKEW_SECONDS = 300


def _safe_status(value: Any) -> bool:
    sensitive = ("secret", "token", "password", "credential", "api_key", "private_key", "authorization", "cookie", "environment_dump")
    def walk(node: Any, key: str = "") -> bool:
        if any(part in key.lower() for part in sensitive):
            return False
        if isinstance(node, dict):
            return all(isinstance(k, str) and walk(v, k) for k, v in node.items())
        if isinstance(node, list):
            return all(walk(v, key) for v in node)
        if isinstance(node, float):
            return math.isfinite(node)
        return node is None or isinstance(node, (str, int, bool))
    if not isinstance(value, dict) or not walk(value):
        return False
    for key in ("cpu", "memory", "latest", "containers", "disk"):
        if key in value and value[key] is not None and not isinstance(value[key], dict):
            return False
    cpu = value.get("cpu")
    if isinstance(cpu, dict):
        for key in ("average_percent", "peak_sustained_5m_percent", "max_one_minute_percent", "utilization_percent"):
            if key in cpu and cpu[key] is not None:
                if isinstance(cpu[key], bool) or not isinstance(cpu[key], (int, float)):
                    return False
                try:
                    if not math.isfinite(float(cpu[key])):
                        return False
                except (OverflowError, ValueError):
                    return False
    for container in (value.get("latest", {}).get("disk"), value.get("disk")):
        if container is None:
            continue
        if not isinstance(container, dict):
            return False
        for key in ("capacity_bytes", "available_bytes"):
            if key in container and (isinstance(container[key], bool) or not isinstance(container[key], int) or container[key] < 0):
                return False
        for key in ("used_percent", "inode_used_percent"):
            if key in container and container[key] is not None:
                if isinstance(container[key], bool) or not isinstance(container[key], (int, float)):
                    return False
                try:
                    if not math.isfinite(float(container[key])) or not 0 <= float(container[key]) <= 100:
                        return False
                except (OverflowError, ValueError):
                    return False
    return True


def load_resource_status(path: Path | None = None, *, now: datetime | None = None) -> dict[str, Any]:
    target = path or Path(os.environ.get("PAPER_AGENT_RESOURCE_STATUS_FILE", "data/resource-status.json"))
    unknown: dict[str, Any] = {"schema": SCHEMA, "state": "unknown", "observed_at": None, "age_seconds": None, "sample_count": 0, "coverage_seconds": 0}
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {**unknown, "state": "unavailable"}
    if not isinstance(value, dict) or value.get("schema") != SCHEMA or not isinstance(value.get("observed_at"), str) or not _safe_status(value):
        return {**unknown, "state": "malformed"}
    current = now or datetime.now(timezone.utc)
    try:
        observed = datetime.fromisoformat(value["observed_at"].replace("Z", "+00:00"))
        delta = (current - observed).total_seconds()
        if delta < -FUTURE_SKEW_SECONDS:
            return {**unknown, "state": "malformed"}
        age = max(0, int(delta))
    except (TypeError, ValueError):
        return {**unknown, "state": "malformed"}
    state = str(value.get("state", "observed"))
    if age > 45 * 60:
        state = "stale"
    return {**value, "state": state, "age_seconds": age}
