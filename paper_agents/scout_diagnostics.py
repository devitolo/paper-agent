"""Local, run-specific Scout troubleshooting reports; never repeat source requests."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sqlite3

SOURCES = {"arxiv", "openalex", "semantic_scholar"}


def _rows(connection, query, params=()):
    cursor = connection.execute(query, params)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def build_report(connection, run_id: int, *, phase: str = "requested") -> dict | None:
    runs = _rows(connection, "SELECT * FROM scout_runs WHERE id = ?", (run_id,))
    if not runs:
        return None
    run = runs[0]
    candidates = _rows(connection, """
        SELECT c.id AS candidate_id, c.paper_id, c.retrieval_order, c.is_new,
               c.excluded, c.exclusion_reason, c.source_query, c.source_diagnostics_json,
               p.title
        FROM scout_candidates c JOIN papers p ON p.id = c.paper_id
        WHERE c.scout_run_id = ? ORDER BY c.retrieval_order, c.id
    """, (run_id,))
    curator = _rows(connection, """
        SELECT id, created_at, scout_attempt_count, requested_rescout, rescout_reason, metadata_json
        FROM curator_runs
        WHERE workflow_cycle_id = ? AND scout_attempt_count >= ? ORDER BY id
    """, (run["workflow_cycle_id"], run["attempt_number"]))
    recommendations = connection.execute("""
        SELECT COUNT(DISTINCT r.id) FROM recommendations r
        JOIN curator_evaluations ce ON ce.curator_run_id = r.curator_run_id AND ce.paper_id = r.paper_id
        JOIN scout_candidates sc ON sc.id = ce.scout_candidate_id
        WHERE sc.scout_run_id = ?
    """, (run_id,)).fetchone()[0]
    return {
        "report_version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "capture_phase": phase,
        "run": run,
        "counts": {"candidates": len(candidates),
                   "eligible": sum(not row["excluded"] for row in candidates),
                   "excluded": sum(bool(row["excluded"]) for row in candidates),
                   "recommendations": recommendations},
        "exclusion_reasons": dict(Counter(row["exclusion_reason"] or "unspecified"
                                          for row in candidates if row["excluded"])),
        "curator_runs": curator,
        "candidates": candidates,
    }


def capture_report(connection, run_id: int, *, phase: str) -> None:
    """Best-effort capture in the caller's transaction, isolated from pipeline work."""
    savepoint_started = False
    try:
        connection.execute("SAVEPOINT scout_diagnostic_capture")
        savepoint_started = True
        report = build_report(connection, run_id, phase=phase)
        if report and report["run"]["source"] in SOURCES and report["run"]["completed_at"]:
            counts = report["counts"]
            # Once captured, keep the report current even after Curator recovery.
            existing = connection.execute(
                "SELECT 1 FROM scout_diagnostic_reports WHERE scout_run_id = ?", (run_id,)
            ).fetchone()
            if (existing or counts["candidates"] <= 2 or counts["eligible"] < 10
                    or (phase != "scout_complete" and counts["recommendations"] == 0)
                    or report["run"]["errors_json"] != "[]"):
                connection.execute("""
                    INSERT INTO scout_diagnostic_reports (scout_run_id, report_json)
                    VALUES (?, ?) ON CONFLICT(scout_run_id) DO UPDATE SET report_json = excluded.report_json
                """, (run_id, json.dumps(report, ensure_ascii=False)))
        connection.execute("RELEASE scout_diagnostic_capture")
    except Exception:
        if savepoint_started:
            try:
                connection.execute("ROLLBACK TO scout_diagnostic_capture")
                connection.execute("RELEASE scout_diagnostic_capture")
            except sqlite3.Error:
                # SQLite may already have rolled back on an I/O failure.
                pass
        logging.getLogger(__name__).exception("Could not capture diagnostics for Scout run %s", run_id)


def load_report(db_path: Path, run_id: int) -> dict | None:
    # Old runs have no snapshot: reconstruct from recorded rows without writing or rerunning Scout.
    with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("BEGIN")
        table = connection.execute("SELECT 1 FROM sqlite_master WHERE name='scout_diagnostic_reports'").fetchone()
        stored = connection.execute("SELECT report_json FROM scout_diagnostic_reports WHERE scout_run_id=?", (run_id,)).fetchone() if table else None
        return json.loads(stored[0]) if stored else build_report(connection, run_id)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/paper_agent.db"))
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--run-id", type=int)
    selector.add_argument("--source", help="Select the latest recorded run for a source")
    args = parser.parse_args()
    run_id = args.run_id
    if args.source:
        with closing(sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            row = connection.execute("SELECT id FROM scout_runs WHERE source=? ORDER BY id DESC LIMIT 1", (args.source,)).fetchone()
            run_id = row[0] if row else 0
    report = load_report(args.db, run_id)
    if report is None:
        parser.error("Scout run not found")
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
