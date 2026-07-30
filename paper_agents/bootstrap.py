from __future__ import annotations

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
