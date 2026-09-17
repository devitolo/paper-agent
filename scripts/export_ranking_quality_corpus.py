#!/usr/bin/env python3
"""Export a minimal, read-only ranking replay fixture from a Project Paper DB."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

# Keep the documented direct script invocation working from the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paper_agents.ranking_quality_replay import canonical_hash, paths_collide


def _triage_text(path: str | None) -> tuple[str, str]:
    if not path:
        return "", "unavailable"
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return "", "unavailable"
    merged = payload.get("merged") if isinstance(payload, dict) else None
    if not isinstance(merged, dict):
        return "", "unavailable"
    sections = []
    for heading, key in (
        ("Research problem", "research_problem"),
        ("Why it matters", "why_it_matters"),
        ("Approach", "approach"),
        ("Source abstract", "source_abstract"),
    ):
        value = str(merged.get(key) or "").strip()
        if value:
            sections.append(f"{heading}\n\n{value}")
    return "\n\n".join(sections), "stored_triage_summary" if sections else "unavailable"


def export_fixture(
    db_path: Path, *, development_ids: set[int], heldout_ids: set[int],
    assign_unlisted: str = "unassigned",
) -> dict[str, Any]:
    if development_ids & heldout_ids:
        raise ValueError("a paper cannot be both development and heldout")
    uri = f"file:{db_path.resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        profile_row = connection.execute(
            "SELECT id, version, profile_json FROM profile_versions WHERE active=1 ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if profile_row is None:
            raise ValueError("database has no active profile")
        profile = json.loads(profile_row["profile_json"])
        rows = connection.execute(
            """
            WITH latest_feedback AS (
                SELECT sf.*, ROW_NUMBER() OVER (PARTITION BY sf.paper_id ORDER BY sf.id DESC) AS ordinal
                FROM structured_feedback sf
                WHERE sf.paper_id IS NOT NULL AND (sf.decision IS NOT NULL OR sf.score IS NOT NULL)
            )
            SELECT p.id, p.title, p.abstract, p.published, lf.decision, lf.score,
                   ps.source, ps.source_id,
                   (SELECT a.path FROM artifacts a
                    WHERE a.paper_id=p.id AND a.artifact_type='triage_summary'
                    ORDER BY a.id DESC LIMIT 1) AS triage_path,
                   (SELECT a.id FROM artifacts a
                    WHERE a.paper_id=p.id AND a.artifact_type='triage_summary'
                    ORDER BY a.id DESC LIMIT 1) AS triage_artifact_id,
                   (SELECT a.metadata_json FROM artifacts a
                    WHERE a.paper_id=p.id AND a.artifact_type='triage_summary'
                    ORDER BY a.id DESC LIMIT 1) AS triage_metadata,
                   EXISTS(SELECT 1 FROM artifacts a
                          WHERE a.paper_id=p.id AND a.artifact_type='pdf') AS pdf_artifact,
                   (SELECT cr.metadata_json
                    FROM curator_evaluations ce JOIN curator_runs cr ON cr.id=ce.curator_run_id
                    WHERE ce.paper_id=p.id ORDER BY ce.id DESC LIMIT 1) AS curator_metadata
            FROM latest_feedback lf
            JOIN papers p ON p.id=lf.paper_id
            LEFT JOIN paper_sources ps ON ps.id=(
                SELECT ps2.id FROM paper_sources ps2 WHERE ps2.paper_id=p.id ORDER BY ps2.id LIMIT 1
            )
            WHERE lf.ordinal=1
            ORDER BY p.id
            """
        ).fetchall()
    finally:
        connection.close()

    candidates = []
    partitions = {"development": [], "heldout": [], "unassigned": []}
    for row in rows:
        paper_id = int(row["id"])
        document_text, evidence_scope = _triage_text(row["triage_path"])
        if not document_text and row["abstract"]:
            document_text, evidence_scope = f"Source abstract\n\n{row['abstract']}", "source_abstract"
        partition = "development" if paper_id in development_ids else (
            "heldout" if paper_id in heldout_ids else assign_unlisted
        )
        partitions[partition].append(str(paper_id))
        triage_metadata = json.loads(row["triage_metadata"] or "{}")
        abstract_only = bool(
            triage_metadata.get("abstract_only")
            or triage_metadata.get("source_type") == "source_abstract"
            or triage_metadata.get("full_text_available") is False
        )
        evidence = {
            "pdf_artifact": bool(row["pdf_artifact"]),
            "triage_artifact_id": row["triage_artifact_id"],
            "abstract_only": abstract_only,
            "full_text_triage": bool(row["triage_artifact_id"]) and not abstract_only
                                and (bool(row["pdf_artifact"])
                                     or triage_metadata.get("full_text_available") is True),
        }
        metadata = json.loads(row["curator_metadata"] or "{}")
        recorded = metadata.get("evaluations", {}).get(str(paper_id), {}) if isinstance(metadata, dict) else {}
        candidate = {
            "id": str(paper_id), "title": row["title"], "abstract": row["abstract"],
            "published": row["published"], "source": row["source"], "source_id": row["source_id"],
            "decision": row["decision"], "user_score": row["score"],
            "document_text": document_text, "evidence_scope": evidence_scope,
            "proposed_assessment": {},
            "evidence": evidence,
        }
        if isinstance(recorded, dict) and isinstance(recorded.get("evidence_assessment"), dict):
            candidate["evidence_assessment"] = recorded["evidence_assessment"]
        candidates.append(candidate)
    fixture = {
        "schema_version": 1,
        "corpus_id": f"sanitized-reviewed-profile-{profile_row['version']}",
        "profile_id": f"profile-version-{profile_row['version']}-db-id-{profile_row['id']}",
        "profile": profile, "profile_sha256": canonical_hash(profile),
        "model_config": {"mode": "stored_judgments", "calls": 0},
        "budgets": {"max_total_chars": 7000, "max_section_chars": 2000, "max_passages": 8},
        "evaluation": {"top_k": 3, "useful_min_score": 3},
        "partitions": partitions, "candidates": candidates,
        "export_notes": [
            "Raw feedback, preference observations, authors, URLs and local paths are excluded.",
            "Unassigned labels must be frozen into development or heldout before tuning or evaluation.",
            "Known development anchors must not be placed in heldout.",
            "Stored triage summaries and abstracts do not imply full-paper coverage.",
        ],
    }
    fixture["candidates_sha256"] = canonical_hash(fixture["candidates"])
    return fixture


def main() -> None:
    parser = argparse.ArgumentParser(description="Export sanitized reviewed corpus for offline ranking replay")
    parser.add_argument("--db", type=Path, default=Path("data/paper_agent.db"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--development-paper-id", type=int, action="append", default=[])
    parser.add_argument("--heldout-paper-id", type=int, action="append", default=[])
    parser.add_argument("--assign-unlisted", choices=("development", "heldout", "unassigned"),
                        default="unassigned")
    args = parser.parse_args()
    if paths_collide(args.db, args.output):
        parser.error("--output must not overwrite --db")
    fixture = export_fixture(
        args.db, development_ids=set(args.development_paper_id), heldout_ids=set(args.heldout_paper_id),
        assign_unlisted=args.assign_unlisted,
    )
    args.output.write_text(json.dumps(fixture, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {len(fixture['candidates'])} reviewed papers to {args.output}")


if __name__ == "__main__":
    main()
