from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from paper_agents import db
from paper_agents.store import DEFAULT_PROFILE_PATH, load_profile

KNOWN_PAPERS = [
    {
        "source": "arxiv",
        "source_id": "2301.03797v2",
        "title": "Recommending Root-Cause and Mitigation Steps for Cloud Incidents using Large Language Models",
        "url": "https://arxiv.org/abs/2301.03797",
        "pdf_url": "https://arxiv.org/pdf/2301.03797",
        "published": "2023-01-10",
        "updated": "2023-02-09",
        "arxiv_id": "2301.03797v2",
        "doi": "10.1109/ICSE48619.2023.00149",
        "authors": [
            "Toufique Ahmed",
            "Supriyo Ghosh",
            "Chetan Bansal",
            "Thomas Zimmermann",
            "Xuchao Zhang",
            "Saravan Rajmohan",
        ],
        "categories": ["cs.SE"],
        "abstract": "Backfilled seed paper for the Project Paper workflow: using large language models to recommend root causes and mitigation steps for cloud incidents.",
        "metadata": {
            "backfill_reason": "Reviewed before the Project Paper database existed",
            "venue": "ICSE 2023",
        },
    }
]


def bootstrap_known_papers(
    *,
    db_path: Path = db.DEFAULT_DB_PATH,
    profile_path: Path = DEFAULT_PROFILE_PATH,
) -> dict[str, Any]:
    db.init_db(db_path)
    profile = load_profile(profile_path)
    inserted: list[dict[str, Any]] = []
    with db.connect_db(db_path) as connection:
        cycle_id = db.create_workflow_cycle(
            connection,
            mode="bootstrap",
            max_scout_attempts=1,
            metadata={"reason": "known-paper backfill"},
        )
        profile_version_id = db.ensure_profile_version(connection, profile)
        db.update_workflow_state(connection, cycle_id, "scouting")
        scout_run_id = db.insert_scout_run(
            connection,
            workflow_cycle_id=cycle_id,
            attempt_number=1,
            source="manual_backfill",
            target_candidates=len(KNOWN_PAPERS),
            max_candidates=len(KNOWN_PAPERS),
            freshness_months=0,
            topics=["manual backfill", "incident management", "root cause analysis"],
            guidance_id=None,
            diagnostics={"bootstrap": True},
        )
        for index, paper in enumerate(KNOWN_PAPERS, 1):
            paper_id, is_new = db.upsert_paper(connection, paper)
            scout_candidate_id = db.insert_scout_candidate(
                connection,
                scout_run_id=scout_run_id,
                paper_id=paper_id,
                retrieval_order=index,
                is_new=is_new,
                excluded=False,
                source_query="manual backfill",
                source_diagnostics={"bootstrap": True},
            )
            inserted.append({"paper_id": paper_id, "scout_candidate_id": scout_candidate_id, **paper})
        db.complete_scout_run(connection, scout_run_id, diagnostics={"stored_count": len(inserted)})
        db.update_workflow_state(connection, cycle_id, "curating")
        curator_run_id = db.create_curator_run(
            connection,
            workflow_cycle_id=cycle_id,
            profile_version_id=profile_version_id,
            scout_attempt_count=1,
            max_scout_attempts=1,
            min_quality_score=1,
            max_recommendations=min(3, len(inserted)),
            model="manual-bootstrap",
            metadata={"bootstrap": True},
        )
        for index, paper in enumerate(inserted, 1):
            db.insert_curator_evaluation(
                connection,
                curator_run_id=curator_run_id,
                paper_id=paper["paper_id"],
                scout_candidate_id=paper["scout_candidate_id"],
                score=100.0,
                rationale="Manual backfill of a previously reviewed seed paper.",
                matched_signals=["incident management", "root cause", "mitigation", "cloud", "llm"],
                quality_threshold_met=True,
            )
            db.insert_recommendation(
                connection,
                curator_run_id=curator_run_id,
                paper_id=paper["paper_id"],
                recommendation_order=index,
                rationale="Manual backfill of a previously reviewed seed paper.",
            )
        db.create_scouting_guidance(
            connection,
            curator_run_id=curator_run_id,
            guidance_text="Prioritize applied papers about AI-assisted incident management, root cause analysis, mitigation, and production operations.",
            metadata={"bootstrap": True},
            active=True,
        )
        db.update_workflow_state(connection, cycle_id, "awaiting_manual_discussion")
    return {
        "db_path": str(db_path),
        "workflow_cycle_id": cycle_id,
        "scout_run_id": scout_run_id,
        "curator_run_id": curator_run_id,
        "paper_count": len(inserted),
        "papers": [{"title": paper["title"], "source_id": paper["source_id"], "url": paper["url"]} for paper in inserted],
    }



def import_legacy_scout_files(
    paths: list[Path],
    *,
    db_path: Path = db.DEFAULT_DB_PATH,
    profile_path: Path = DEFAULT_PROFILE_PATH,
    recommend_all_if_unselected: bool = False,
) -> dict[str, Any]:
    db.init_db(db_path)
    profile = load_profile(profile_path)
    imported_runs: list[dict[str, Any]] = []
    with db.connect_db(db_path) as connection:
        profile_version_id = db.ensure_profile_version(connection, profile)
        for path in paths:
            candidates = read_jsonl_candidates(path)
            selected = [candidate for candidate in candidates if candidate.get("selected")]
            if recommend_all_if_unselected and not selected:
                selected = candidates[:3]
            cycle_id = db.create_workflow_cycle(
                connection,
                mode="legacy_import",
                max_scout_attempts=1,
                metadata={"source_file": str(path)},
            )
            db.update_workflow_state(connection, cycle_id, "scouting")
            db.increment_scout_attempts(connection, cycle_id)
            scout_run_id = db.insert_scout_run(
                connection,
                workflow_cycle_id=cycle_id,
                attempt_number=1,
                source="legacy_jsonl",
                target_candidates=len(candidates),
                max_candidates=len(candidates),
                freshness_months=0,
                topics=["legacy import"],
                guidance_id=None,
                diagnostics={"source_file": str(path)},
            )
            imported: list[dict[str, Any]] = []
            for index, candidate in enumerate(candidates, 1):
                normalized = normalize_legacy_candidate(candidate)
                paper_id, is_new = db.upsert_paper(connection, normalized)
                scout_candidate_id = db.insert_scout_candidate(
                    connection,
                    scout_run_id=scout_run_id,
                    paper_id=paper_id,
                    retrieval_order=index,
                    is_new=is_new,
                    excluded=False,
                    source_query=(normalized.get("metadata") or {}).get("query_topic"),
                    source_diagnostics={"legacy_score": candidate.get("score")},
                )
                imported.append({"paper_id": paper_id, "scout_candidate_id": scout_candidate_id, **normalized})
                if normalized.get("pdf_path"):
                    db.insert_artifact(
                        connection,
                        paper_id,
                        artifact_type="pdf",
                        path=Path(normalized["pdf_path"]),
                        metadata={"legacy_import": True, "pdf_url": normalized.get("pdf_url")},
                    )
            db.complete_scout_run(connection, scout_run_id, diagnostics={"stored_count": len(imported)})
            db.update_workflow_state(connection, cycle_id, "curating")
            selected_keys = {legacy_selection_key(candidate) for candidate in selected}
            selected_imported = [item for item in imported if legacy_selection_key(item) in selected_keys]
            curator_run_id = db.create_curator_run(
                connection,
                workflow_cycle_id=cycle_id,
                profile_version_id=profile_version_id,
                scout_attempt_count=1,
                max_scout_attempts=1,
                min_quality_score=1,
                max_recommendations=min(3, max(1, len(selected_imported))),
                model="legacy-import",
                metadata={"source_file": str(path)},
            )
            for item in imported:
                was_selected = legacy_selection_key(item) in selected_keys
                raw_score = item.get("score")
                score = float(raw_score if raw_score is not None else (100.0 if was_selected else 0.0))
                db.insert_curator_evaluation(
                    connection,
                    curator_run_id=curator_run_id,
                    paper_id=item["paper_id"],
                    scout_candidate_id=item["scout_candidate_id"],
                    score=score,
                    rationale=item.get("ranking_reason") or "Imported from legacy Scout JSONL.",
                    matched_signals=item.get("matched_keywords") or [],
                    quality_threshold_met=was_selected,
                )
            for order, item in enumerate(selected_imported[:3], 1):
                db.insert_recommendation(
                    connection,
                    curator_run_id=curator_run_id,
                    paper_id=item["paper_id"],
                    recommendation_order=order,
                    rationale=item.get("ranking_reason") or "Imported from legacy selected Scout result.",
                )
            db.update_workflow_state(connection, cycle_id, "awaiting_manual_discussion")
            imported_runs.append(
                {
                    "source_file": str(path),
                    "workflow_cycle_id": cycle_id,
                    "scout_run_id": scout_run_id,
                    "curator_run_id": curator_run_id,
                    "candidate_count": len(imported),
                    "recommendation_count": len(selected_imported[:3]),
                }
            )
    return {"db_path": str(db_path), "runs": imported_runs}


def read_jsonl_candidates(path: Path) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"Could not parse {path}:{line_number}: {error}") from error
        if isinstance(value, dict):
            candidates.append(value)
    return candidates


def normalize_legacy_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(candidate)
    source = normalized.get("source") or "arxiv"
    source_id = normalized.get("source_id") or source_id_from_url(normalized.get("url"))
    normalized["source"] = source
    normalized["source_id"] = source_id or normalized.get("title") or "unknown"
    normalized.setdefault("published", normalized.get("publication_date"))
    if source == "arxiv":
        normalized.setdefault("arxiv_id", normalized["source_id"])
    return normalized


def source_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    marker = "arxiv.org/abs/"
    if marker in url:
        return url.split(marker, 1)[1].split("?", 1)[0]
    return None


def legacy_selection_key(candidate: dict[str, Any]) -> str:
    return f"{candidate.get('source')}:{candidate.get('source_id')}"
