#!/usr/bin/env python3
"""Bounded, read-only host resource collector for Project Paper.

The one-minute systemd timer calls ``collect``.  It appends sanitized samples,
emits a 15-minute summary, and atomically publishes the latest status.  It
does not access the application database, credentials, providers, or Docker
unless an explicitly configured container-name list is present.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any


DEFAULT_ROOT = Path("/var/lib/project-paper-observability/mini-nonprod")
SCHEMA = "project-paper.resource-status.v1"


def now() -> datetime:
    return datetime.now(timezone.utc)


def read_meminfo() -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, value = line.partition(":")
            parts = value.strip().split()
            if parts and parts[0].isdigit():
                values[key] = int(parts[0]) * (1024 if len(parts) > 1 and parts[1] == "kB" else 1)
    except OSError:
        pass
    return values


def read_cpu() -> tuple[int, int] | None:
    try:
        fields = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
        numbers = [int(value) for value in fields]
        return sum(numbers), numbers[3] + (numbers[4] if len(numbers) > 4 else 0)
    except (OSError, IndexError, ValueError):
        return None


def filesystem(path: Path) -> dict[str, Any]:
    try:
        stat = os.statvfs(path)
        capacity = stat.f_blocks * stat.f_frsize
        available = stat.f_bavail * stat.f_frsize
        inode_capacity = stat.f_files
        inode_available = stat.f_favail
        return {"path": str(path), "capacity_bytes": capacity, "available_bytes": available,
                "used_percent": round((capacity - available) * 100 / capacity, 2) if capacity else None,
                "inode_used_percent": round((inode_capacity - inode_available) * 100 / inode_capacity, 2) if inode_capacity else None}
    except OSError as exc:
        return {"path": str(path), "error": type(exc).__name__}


def temperature() -> dict[str, Any]:
    for path in sorted(Path("/sys/class/thermal").glob("thermal_zone*/temp")):
        try:
            return {"celsius": round(int(path.read_text().strip()) / 1000, 1), "source": str(path)}
        except (OSError, ValueError):
            continue
    return {"state": "unavailable"}


def containers() -> dict[str, Any]:
    names = [name.strip() for name in os.environ.get("PROJECT_PAPER_CONTAINER_NAMES", "").split(",") if name.strip()]
    if not names:
        return {"state": "unconfigured"}
    result: list[dict[str, Any]] = []
    for name in names:
        try:
            output = subprocess.check_output(["docker", "inspect", "--format", "{{json .}}", name], text=True, timeout=3)
            inspected = json.loads(output)
            state = inspected.get("State") or {}
            config = inspected.get("Config") or {}
            labels = config.get("Labels") or {}
            result.append({"name": name, "status": state.get("Status", "unknown"), "health": (state.get("Health") or {}).get("Status", "unknown"), "restart_count": state.get("RestartCount", 0), "oom_killed": bool(state.get("OOMKilled", False)), "image": config.get("Image", "unknown"), "revision": labels.get("org.opencontainers.image.revision", "unknown")})
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            result.append({"name": name, "state": "unavailable"})
    return {"state": "observed", "items": result}


def sample(root: Path) -> dict[str, Any]:
    stamp = now()
    cpu = read_cpu()
    mem = read_meminfo()
    record: dict[str, Any] = {"schema": SCHEMA, "collector_version": "2", "observed_at": stamp.isoformat(), "host": os.environ.get("PROJECT_PAPER_RESOURCE_HOST", "unknown"), "environment": os.environ.get("PROJECT_PAPER_RESOURCE_ENVIRONMENT", "mini-nonprod"), "scope": "host", "memory": {"available_bytes": mem.get("MemAvailable"), "total_bytes": mem.get("MemTotal"), "swap_total_bytes": mem.get("SwapTotal"), "swap_free_bytes": mem.get("SwapFree"), "swap_used_bytes": (mem.get("SwapTotal") - mem.get("SwapFree")) if mem.get("SwapTotal") is not None and mem.get("SwapFree") is not None else None}, "cpu": {"total_jiffies": cpu[0], "idle_jiffies": cpu[1]} if cpu else {"state": "unavailable"}, "containers": containers(), "temperature": temperature()}
    previous_path = root / "last-cpu.json"
    if cpu:
        try:
            previous = json.loads(previous_path.read_text())
            total_delta = cpu[0] - int(previous["total"])
            idle_delta = cpu[1] - int(previous["idle"])
            if total_delta > 0:
                record["cpu"]["utilization_percent"] = round(max(0.0, min(100.0, (total_delta - idle_delta) * 100 / total_delta)), 2)
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            pass
        previous_path.write_text(json.dumps({"total": cpu[0], "idle": cpu[1]}))
    if stamp.minute % 60 == 0:
        record["disk"] = filesystem(Path(os.environ.get("PROJECT_PAPER_RESOURCE_DISK_PATH", "/")))
    cgroup = Path("/sys/fs/cgroup")
    try:
        record["cgroup"] = {"memory_current_bytes": int((cgroup / "memory.current").read_text().strip()), "memory_max_bytes": (lambda value: None if value == "max" else int(value))((cgroup / "memory.max").read_text().strip())}
    except (OSError, ValueError):
        record["cgroup"] = {"state": "unavailable"}
    return record


def append_bounded(path: Path, record: dict[str, Any], limit: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, separators=(",", ":")) + "\n")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
        path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    except OSError:
        pass


def summarize(root: Path) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    try:
        for line in (root / "samples.jsonl").read_text(encoding="utf-8").splitlines()[-15:]:
            try:
                value = json.loads(line)
                if isinstance(value, dict) and isinstance(value.get("observed_at"), str):
                    records.append(value)
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    cpu = [float(row["cpu"]["utilization_percent"]) for row in records if row.get("cpu", {}).get("utilization_percent") is not None]
    available = [int(row["memory"]["available_bytes"]) for row in records if row.get("memory", {}).get("available_bytes") is not None]
    summary = {"schema": SCHEMA, "collector_version": "2", "observed_at": now().isoformat(), "host": os.environ.get("PROJECT_PAPER_RESOURCE_HOST", "unknown"), "environment": os.environ.get("PROJECT_PAPER_RESOURCE_ENVIRONMENT", "mini-nonprod"), "scope": "host", "window": {"end_at": now().replace(second=0, microsecond=0).isoformat(), "interval_seconds": 60, "complete": len(records) >= 15}, "sample_count": len(records), "coverage_seconds": max(0, (len(records) - 1) * 60), "cpu": {"average_percent": round(sum(cpu) / len(cpu), 2) if cpu else None, "peak_sustained_5m_percent": round(max(sum(cpu[i:i + 5]) / len(cpu[i:i + 5]) for i in range(max(1, len(cpu) - 4))), 2) if cpu else None, "max_one_minute_percent": max(cpu) if cpu else None}, "memory": {"available_average_bytes": round(sum(available) / len(available)) if available else None, "available_min_bytes": min(available) if available else None}, "errors": []}
    return summary


def publish(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="resource-status-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, separators=(",", ":"))
            stream.write("\n")
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def collect(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    current = sample(root)
    append_bounded(root / "samples.jsonl", current, 10080)
    if now().minute % 15 == 0:
        summary = summarize(root)
        append_bounded(root / "summaries.jsonl", summary, 8640)
        publish(root / "status.json", summary)
    elif not (root / "status.json").exists():
        publish(root / "status.json", {**summarize(root), "state": "warming"})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["collect", "sample", "summary"])
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("PROJECT_PAPER_RESOURCE_ROOT", DEFAULT_ROOT)))
    args = parser.parse_args()
    if args.command == "collect": collect(args.root)
    elif args.command == "sample": print(json.dumps(sample(args.root), indent=2))
    else: print(json.dumps(summarize(args.root), indent=2))


if __name__ == "__main__":
    main()
