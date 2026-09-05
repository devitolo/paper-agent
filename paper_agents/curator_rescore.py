"""Explicit, auditable rescoring of existing recommendations; no new retrieval."""
from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
import sqlite3
from typing import Any

from paper_agents import db
from paper_agents.curator_scoring import SCORING_VERSION, evaluate_candidate


def rescore_recommendations(db_path: Path, recommendation_date: str, *, apply: bool = False,
                            source: str | None = None) -> dict[str, Any]:
    day = date.fromisoformat(recommendation_date).isoformat()
    path = db_path.resolve()
    if not path.is_file():
        raise RuntimeError(f"Database does not exist: {path}")
    connection = (db.connect_db(path) if apply else
                  sqlite3.connect(path.as_uri() + "?mode=ro", uri=True))
    with closing(connection), connection:
        if apply:
            connection.execute("BEGIN IMMEDIATE")
        profile_version = db.current_profile_version(connection)
        if profile_version is None:
            raise RuntimeError("No active profile is available for rescoring")
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT r.id AS recommendation_id, r.curator_run_id, r.paper_id,
                   ce.id AS evaluation_id, ce.score AS old_score, ce.rationale AS old_rationale,
                   ce.matched_signals_json AS old_signals, ce.quality_threshold_met AS old_quality,
                   r.rationale AS old_recommendation_rationale,
                   cr.min_quality_score, p.title, p.abstract
            FROM recommendations r JOIN papers p ON p.id = r.paper_id
            JOIN curator_runs cr ON cr.id = r.curator_run_id
            JOIN curator_evaluations ce ON ce.curator_run_id = r.curator_run_id AND ce.paper_id = r.paper_id
            WHERE date(r.created_at) = ?
              AND (? IS NULL OR EXISTS (SELECT 1 FROM paper_sources ps WHERE ps.paper_id = p.id AND ps.source = ?))
            ORDER BY r.id
            """, (day, source, source),
        ).fetchall()
        results = []
        changed_count = 0
        timestamp = datetime.now(timezone.utc).isoformat()
        for row in rows:
            evaluation = evaluate_candidate(
                {"paper_id": row["paper_id"], "title": row["title"], "abstract": row["abstract"],
                 "evidence": db.paper_evidence_context(connection, row["paper_id"])}, profile_version["profile"],
            )
            quality_met = evaluation["score"] >= row["min_quality_score"]
            metadata = db.decode_json(connection.execute(
                "SELECT metadata_json FROM curator_runs WHERE id = ?", (row["curator_run_id"],),
            ).fetchone()[0], {})
            last_rescore = metadata.get("rescored_evaluations", {}).get(str(row["evaluation_id"]), {})
            changed = (row["old_score"] != evaluation["score"] or row["old_rationale"] != evaluation["rationale"]
                       or last_rescore.get("profile_version_id") != profile_version["id"])
            changed_count += int(changed)
            if apply and changed:
                metadata.setdefault("rescore_history", []).append({
                    "at": timestamp, "version": SCORING_VERSION, "profile_version_id": profile_version["id"],
                    "evaluation_id": row["evaluation_id"], "recommendation_id": row["recommendation_id"],
                    "previous": {"score": row["old_score"], "rationale": row["old_rationale"],
                                 "matched_signals": db.decode_json(row["old_signals"], []),
                                 "quality_threshold_met": row["old_quality"],
                                 "recommendation_rationale": row["old_recommendation_rationale"]},
                    "score": evaluation["score"], "components": evaluation["score_components"],
                })
                metadata.setdefault("rescored_evaluations", {})[str(row["evaluation_id"])] = {
                    "profile_version_id": profile_version["id"], "version": SCORING_VERSION,
                    "components": evaluation["score_components"], "at": timestamp,
                }
                connection.execute("UPDATE curator_runs SET metadata_json = ? WHERE id = ?",
                                   (db.json_dumps(metadata), row["curator_run_id"]))
                connection.execute(
                    "UPDATE curator_evaluations SET score = ?, rationale = ?, matched_signals_json = ?, "
                    "quality_threshold_met = ? WHERE id = ?",
                    (evaluation["score"], evaluation["rationale"], db.json_dumps(evaluation["matched_signals"]),
                     int(quality_met), row["evaluation_id"]),
                )
                connection.execute("UPDATE recommendations SET rationale = ? WHERE id = ?",
                                   (evaluation["rationale"], row["recommendation_id"]))
            results.append({"recommendation_id": row["recommendation_id"], "paper_id": row["paper_id"],
                            "title": row["title"], "old_score": row["old_score"], "score": evaluation["score"],
                            "quality_threshold_met": quality_met, "components": evaluation["score_components"]})
    return {"applied": apply, "date_utc": day, "profile_version_id": profile_version["id"],
            "scoring_version": SCORING_VERSION, "recommendation_count": len(results),
            "changed_count": changed_count, "results": results}
