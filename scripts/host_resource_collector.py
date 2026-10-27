#!/usr/bin/env python3
"""Bounded, read-only host resource collector for Project Paper.

The fifteen-minute cron wrapper calls ``collect``.  It appends sanitized samples,
emits a 15-minute summary, and atomically publishes the latest status.  It
does not access the application database, credentials, providers, or Docker
unless an explicitly configured container-name list is present.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any


DEFAULT_ROOT = Path("/var/lib/project-paper-observability/mini-nonprod")
SCHEMA = "project-paper.resource-status.v1"

def proc_path(name: str) -> Path:
    return Path(os.environ.get("PROJECT_PAPER_RESOURCE_PROC_ROOT", "/proc")) / name

def sys_path(name: str) -> Path:
    return Path(os.environ.get("PROJECT_PAPER_RESOURCE_SYS_ROOT", "/sys")) / name

def boot_id() -> str | None:
    try:
        value = Path(os.environ.get("PROJECT_PAPER_RESOURCE_BOOT_ID_PATH", "/proc/sys/kernel/random/boot_id")).read_text().strip()
        return value if value else None
    except OSError:
        return None


def now() -> datetime:
    return datetime.now(timezone.utc)


def read_meminfo() -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        for line in proc_path("meminfo").read_text().splitlines():
            key, _, value = line.partition(":")
            parts = value.strip().split()
            if parts and parts[0].isdigit():
                values[key] = int(parts[0]) * (1024 if len(parts) > 1 and parts[1] == "kB" else 1)
    except OSError:
        pass
    return values


def read_cpu() -> tuple[int, int] | None:
    try:
        fields = proc_path("stat").read_text().splitlines()[0].split()[1:]
        numbers = [int(value) for value in fields[:8]]
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
    for path in sorted(sys_path("class/thermal").glob("thermal_zone*/temp")):
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
            result.append({"name": name, "status": state.get("Status", "unknown"), "health": (state.get("Health") or {}).get("Status", "unknown"), "restart_count": inspected.get("RestartCount", 0), "oom_killed": bool(state.get("OOMKilled", False)), "image": config.get("Image", "unknown"), "revision": labels.get("org.opencontainers.image.revision", "unknown")})
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            result.append({"name": name, "state": "unavailable"})
    return {"state": "observed", "items": result}


def sample(root: Path) -> dict[str, Any]:
    stamp = now()
    cpu = read_cpu()
    mem = read_meminfo()
    record: dict[str, Any] = {"schema": SCHEMA, "collector_version": "2", "observed_at": stamp.isoformat(), "host": os.environ.get("PROJECT_PAPER_RESOURCE_HOST", "unknown"), "environment": os.environ.get("PROJECT_PAPER_RESOURCE_ENVIRONMENT", "mini-nonprod"), "scope": "host", "boot_id": boot_id(), "memory": {"available_bytes": mem.get("MemAvailable"), "total_bytes": mem.get("MemTotal"), "swap_total_bytes": mem.get("SwapTotal"), "swap_free_bytes": mem.get("SwapFree"), "swap_used_bytes": (mem.get("SwapTotal") - mem.get("SwapFree")) if mem.get("SwapTotal") is not None and mem.get("SwapFree") is not None else None}, "cpu": {"total_jiffies": cpu[0], "idle_jiffies": cpu[1]} if cpu else {"state": "unavailable"}, "containers": containers(), "temperature": temperature()}
    previous_path = root / "last-cpu.json"
    if cpu:
        try:
            previous = json.loads(previous_path.read_text())
            total_delta = cpu[0] - int(previous["total"])
            idle_delta = cpu[1] - int(previous["idle"])
            elapsed = (stamp - datetime.fromisoformat(previous["observed_at"])).total_seconds()
            record["cpu"]["interval_seconds"] = elapsed
            if previous.get("boot_id") and record.get("boot_id") and previous["boot_id"] != record["boot_id"]:
                record["cpu"]["state"] = "boot_changed"
            elif total_delta > 0 and idle_delta >= 0 and idle_delta <= total_delta and 0 < elapsed <= 20 * 60:
                record["cpu"]["utilization_percent"] = round(max(0.0, min(100.0, (total_delta - idle_delta) * 100 / total_delta)), 2)
            elif elapsed > 20 * 60:
                record["cpu"]["state"] = "gap"
            else:
                record["cpu"]["state"] = "baseline_invalid"
        except (OSError, ValueError, TypeError, KeyError, OverflowError, json.JSONDecodeError):
            record["cpu"]["state"] = "baseline_invalid"
        previous_path.write_text(json.dumps({"total": cpu[0], "idle": cpu[1], "observed_at": stamp.isoformat(), "boot_id": record.get("boot_id")}))
    record["disk"] = filesystem(Path(os.environ.get("PROJECT_PAPER_RESOURCE_DISK_PATH", "/")))
    cgroup = Path(os.environ.get("PROJECT_PAPER_RESOURCE_CGROUP_ROOT", "/sys/fs/cgroup"))
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


def aligned_end(value: datetime) -> datetime:
    return value.replace(minute=(value.minute // 15) * 15, second=0, microsecond=0)


def summarize(root: Path, *, reference: datetime | None = None) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    try:
        for line in (root / "samples.jsonl").read_text(encoding="utf-8").splitlines()[-60:]:
            try:
                value = json.loads(line)
                if isinstance(value, dict) and isinstance(value.get("observed_at"), str):
                    records.append(value)
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    end = aligned_end(reference or now())
    start = end.timestamp() - 15 * 60
    window_records = []
    for row in records:
        try:
            observed = datetime.fromisoformat(row["observed_at"].replace("Z", "+00:00"))
            if start <= observed.timestamp() < end.timestamp() and observed.timestamp() <= (reference or now()).timestamp() + 300:
                window_records.append(row)
        except (TypeError, ValueError):
            continue
    records = window_records
    records.sort(key=lambda row: row["observed_at"])
    deduped = {}
    for row in records:
        deduped[row["observed_at"][:16]] = row
    records = list(deduped.values())
    cpu = [float(row["cpu"]["utilization_percent"]) for row in records if isinstance(row.get("cpu"), dict) and isinstance(row["cpu"].get("utilization_percent"), (int, float)) and math.isfinite(float(row["cpu"]["utilization_percent"]))]
    available = [int(row["memory"]["available_bytes"]) for row in records if isinstance(row.get("memory"), dict) and isinstance(row["memory"].get("available_bytes"), (int, float)) and math.isfinite(float(row["memory"]["available_bytes"]))]
    timestamps = []
    for row in records:
        try: timestamps.append(datetime.fromisoformat(row["observed_at"].replace("Z", "+00:00")).timestamp())
        except (TypeError, ValueError): pass
    gaps = sum(1 for left, right in zip(sorted(timestamps), sorted(timestamps)[1:]) if right - left > 20 * 60)
    segments = []
    segment = []
    for row in records:
        value = row.get("cpu", {}).get("utilization_percent") if isinstance(row.get("cpu"), dict) else None
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            value = None
        if not segment:
            segment = [float(value)] if value is not None else []
            continue
        try:
            delta = (datetime.fromisoformat(row["observed_at"].replace("Z", "+00:00")) - datetime.fromisoformat(records[records.index(row)-1]["observed_at"].replace("Z", "+00:00"))).total_seconds()
        except (TypeError, ValueError):
            delta = 999
        if delta > 20 * 60 or value is None:
            if segment: segments.append(segment)
            segment = [float(value)] if value is not None else []
        else:
            segment.append(float(value))
    if segment: segments.append(segment)
    five = []
    for values in segments:
        five.extend(sum(values[i:i + 5]) / 5 for i in range(len(values) - 4))
    latest = records[-1] if records else {}
    try: observations = json.loads((root / "last-observations.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError): observations = {}
    def latest_field(name: str):
        for row in reversed(records):
            if row.get(name) is not None:
                return row.get(name)
        entry = observations.get(name)
        return entry.get("value") if isinstance(entry, dict) else entry
    # Disk is an instantaneous observation, independent of the CPU averaging window.
    # Keep its value and timestamp from the same sample, including a just-collected
    # boundary sample that is outside the preceding averaging window.
    disk_observations = [
        {"observed_at": row["observed_at"], "value": row["disk"]}
        for row in records if row.get("disk") is not None
    ]
    cached_disk = observations.get("disk")
    if isinstance(cached_disk, dict) and cached_disk.get("observed_at"):
        disk_observations.append(cached_disk)
    if disk_observations:
        observations["disk"] = max(disk_observations, key=lambda item: datetime.fromisoformat(item["observed_at"].replace("Z", "+00:00")))
    latest_disk = observations.get("disk", {}).get("value")
    summary = {"schema": SCHEMA, "collector_version": "4", "observed_at": now().isoformat(), "host": os.environ.get("PROJECT_PAPER_RESOURCE_HOST", "unknown"), "environment": os.environ.get("PROJECT_PAPER_RESOURCE_ENVIRONMENT", "mini-nonprod"), "scope": "host", "window": {"start_at": datetime.fromtimestamp(start, timezone.utc).isoformat(), "end_at": end.isoformat(), "interval_seconds": 900, "complete": bool(records) and gaps == 0, "sample_gaps": gaps}, "sample_count": len(records), "coverage_seconds": 900 if records else 0, "cpu": {"average_percent": round(sum(cpu) / len(cpu), 2) if cpu else None, "max_observed_percent": max(cpu) if cpu else None, "peak_sustained_5m_percent": None, "max_one_minute_percent": max(cpu) if cpu else None}, "memory": {"available_average_bytes": round(sum(available) / len(available)) if available else None, "available_max_bytes": max(available) if available else None, "available_min_bytes": min(available) if available else None}, "disk": latest_disk, "latest": {"observed_at": latest.get("observed_at"), "disk": latest_disk, "containers": latest_field("containers"), "temperature": latest_field("temperature"), "cgroup": latest_field("cgroup"), "observation_times": {name: (entry.get("observed_at") if isinstance(entry, dict) else None) for name, entry in observations.items()}}, "errors": []}
    return summary


def publish(path: Path, record: dict[str, Any], *, mode: int = 0o640) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="resource-status-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, separators=(",", ":"))
            stream.write("\n")
            stream.flush(); os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def collect(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    current = sample(root)
    append_bounded(root / "samples.jsonl", current, 672)
    observations = {}
    try: observations = json.loads((root / "last-observations.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError): pass
    for field in ("disk", "containers", "temperature", "cgroup"):
        if current.get(field) is not None:
            observations[field] = {"observed_at": current["observed_at"], "value": current[field]}
    publish(root / "last-observations.json", observations, mode=0o600)
    current_time = datetime.fromisoformat(current["observed_at"])
    if current_time.minute % 15 == 0:
        summary = summarize(root, reference=current_time)
        append_bounded(root / "summaries.jsonl", summary, 8640)
        publish(root / "status.json", summary)
        export_dir = os.environ.get("PROJECT_PAPER_RESOURCE_EXPORT_DIR")
        if export_dir:
            Path(export_dir).mkdir(parents=True, exist_ok=True, mode=0o755)
            publish(Path(export_dir) / "status.json", summary, mode=0o644)
    elif not (root / "status.json").exists():
        warming = {**summarize(root), "state": "warming"}
        publish(root / "status.json", warming)
        export_dir = os.environ.get("PROJECT_PAPER_RESOURCE_EXPORT_DIR")
        if export_dir:
            Path(export_dir).mkdir(parents=True, exist_ok=True, mode=0o755)
            publish(Path(export_dir) / "status.json", warming, mode=0o644)


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
