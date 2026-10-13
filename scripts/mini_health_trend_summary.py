#!/usr/bin/env python3
"""Render stable, secret-safe trend records from a Mini health JSON snapshot."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SOURCE_NAMES = ("arxiv", "openalex", "semantic_scholar", "core", "zenml")


def _category(message: str) -> str:
    lowered = message.lower()
    if "returned 0 candidates" in lowered:
        return "zero_candidates"
    if "only" in lowered and "eligible candidates" in lowered:
        return "underfilled_eligible_pool"
    if "cooldown" in lowered or "too many requests" in lowered or "rate limit" in lowered:
        return "provider_rate_limited"
    if "no workflow cycle" in lowered or "workflow cycle" in lowered:
        return "workflow_cycle_stale"
    return "health_warning"


def _source(message: str, run: dict[str, Any] | None) -> str | None:
    if run and run.get("source"):
        return str(run["source"])
    lowered = message.lower()
    for name in SOURCE_NAMES:
        if name in lowered:
            return name
    return None


def build_records(payload: dict[str, Any], captured_at: str | None = None) -> list[dict[str, Any]]:
    captured = captured_at or datetime.now(timezone.utc).isoformat()
    runs = {}
    for run in payload.get("latest_scout_runs_by_source") or []:
        if run.get("id") is not None:
            runs[str(run["id"])] = run
    records: list[dict[str, Any]] = []
    collection_error = payload.get("collection_error")
    if collection_error:
        records.append(
            {
                "captured_at": captured,
                "level": "critical",
                "category": "workflow_plumbing",
                "source": None,
                "run_id": None,
                "run_time": None,
                "candidates": None,
                "eligible": None,
                "recommendations": None,
                "message": str(collection_error),
                "origin": "workflow_plumbing",
            }
        )
    for warning in payload.get("warnings") or []:
        message = str(warning.get("message") or "")
        run_id = warning.get("scout_run_id")
        run = runs.get(str(run_id)) if run_id is not None else None
        records.append(
            {
                "captured_at": captured,
                "level": str(warning.get("level") or "warning"),
                "category": _category(message),
                "source": _source(message, run),
                "run_id": run_id,
                "run_time": (run or {}).get("started_at"),
                "candidates": (run or {}).get("candidate_count"),
                "eligible": (run or {}).get("eligible_count"),
                "recommendations": (run or {}).get("recommendation_count"),
                "message": message,
                "origin": "production_health",
            }
        )
    return records


def render_table(records: list[dict[str, Any]]) -> str:
    if not records:
        return "No health warnings or collection errors."
    lines = ["LEVEL  CATEGORY                  SOURCE           RUN  ELIGIBLE  RECOMMENDATIONS  ORIGIN"]
    for record in records:
        lines.append(
            f"{record['level']:<6} {record['category']:<25} "
            f"{str(record['source'] or '-'): <16} {str(record['run_id'] or '-'): <4} "
            f"{str(record['eligible'] if record['eligible'] is not None else '-'): <8} "
            f"{str(record['recommendations'] if record['recommendations'] is not None else '-'): <16} "
            f"{record['origin']}"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("health_json", type=Path)
    parser.add_argument("--jsonl", type=Path, required=True)
    args = parser.parse_args()
    payload = json.loads(args.health_json.read_text(encoding="utf-8"))
    records = build_records(payload, captured_at=payload.get("captured_at"))
    args.jsonl.write_text("".join(json.dumps(record, sort_keys=True) + "\n" for record in records), encoding="utf-8")
    print(render_table(records))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
