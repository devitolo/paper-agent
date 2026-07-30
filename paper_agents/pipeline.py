from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any

from paper_agents import db
from paper_agents.curator_agent import (
    DEFAULT_MAX_RECOMMENDATIONS,
    DEFAULT_MIN_QUALITY_SCORE,
    CuratorAgent,
    CuratorConfig,
)
from paper_agents.local_extract import DEFAULT_MODEL, DEFAULT_OLLAMA_URL, extract_paper, output_path_for
from paper_agents.scout import (
    DEFAULT_ARXIV_REQUEST_DELAY,
    DEFAULT_ARXIV_RETRIES,
    DEFAULT_ARXIV_TIMEOUT,
    DEFAULT_FETCH_LIMIT,
    DEFAULT_FRESHNESS_MONTHS,
    DEFAULT_PDF_DIR,
    DEFAULT_SCOUT_DIR,
    DEFAULT_SCOUT_TOPICS,
    safe_filename,
)
from paper_agents.scout_agent import DEFAULT_TARGET_CANDIDATES, ScoutAgent, ScoutConfig
from paper_agents.store import DEFAULT_PROFILE_PATH, load_profile

DEFAULT_PIPELINE_MAX_CHARS = 7000
DEFAULT_PIPELINE_LIMIT_CHUNKS = 0
DEFAULT_PIPELINE_WORKERS = 2
DEFAULT_PIPELINE_TIMEOUT = 600
DEFAULT_MAX_SCOUT_ATTEMPTS = 3


def run_daily_pipeline(
    *,
    topics: list[str] | None = None,
    freshness_months: int = DEFAULT_FRESHNESS_MONTHS,
    fetch_limit: int = DEFAULT_FETCH_LIMIT,
    keep_limit: int = DEFAULT_MAX_RECOMMENDATIONS,
    scout_dir: Path = DEFAULT_SCOUT_DIR,
    pdf_dir: Path = DEFAULT_PDF_DIR,
    model: str = DEFAULT_MODEL,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    max_chars: int = DEFAULT_PIPELINE_MAX_CHARS,
    limit_chunks: int = DEFAULT_PIPELINE_LIMIT_CHUNKS,
    timeout: int = DEFAULT_PIPELINE_TIMEOUT,
    workers: int = DEFAULT_PIPELINE_WORKERS,
    db_path: Path | None = db.DEFAULT_DB_PATH,
    mode: str = "full",
    request_delay: float = DEFAULT_ARXIV_REQUEST_DELAY,
    scout_retries: int = DEFAULT_ARXIV_RETRIES,
    scout_timeout: int = DEFAULT_ARXIV_TIMEOUT,
    include_seen: bool = False,
    max_scout_attempts: int = DEFAULT_MAX_SCOUT_ATTEMPTS,
    min_quality_score: float = DEFAULT_MIN_QUALITY_SCORE,
    profile_path: Path = DEFAULT_PROFILE_PATH,
) -> dict[str, Any]:
    """Run Scout -> Curator -> recommended PDF extraction."""
    if db_path is None:
        raise RuntimeError("pipeline-daily V2 requires SQLite; use scout-daily for throwaway source checks")

    db.init_db(db_path)
    topics_for_run = topics or DEFAULT_SCOUT_TOPICS
    max_recommendations = min(max(1, keep_limit), DEFAULT_MAX_RECOMMENDATIONS)
    max_scout_attempts = max(1, max_scout_attempts)
    profile = load_profile(profile_path)

    scout_results: list[dict[str, Any]] = []
    curator_result: dict[str, Any] | None = None
    cards: list[dict[str, Any]] = []

    with db.connect_db(db_path) as connection:
        cycle_id = db.create_workflow_cycle(
            connection,
            mode=mode,
            max_scout_attempts=max_scout_attempts,
            metadata={
                "topics": topics_for_run,
                "fetch_limit": fetch_limit,
                "max_recommendations": max_recommendations,
            },
        )
        profile_version_id = db.ensure_profile_version(connection, profile)
        profile_version = db.current_profile_version(connection)

        for attempt in range(1, max_scout_attempts + 1):
            scout_result = ScoutAgent().run(
                connection,
                workflow_cycle_id=cycle_id,
                attempt_number=attempt,
                config=ScoutConfig(
                    topics=topics_for_run,
                    target_candidates=DEFAULT_TARGET_CANDIDATES,
                    max_candidates=fetch_limit,
                    freshness_months=freshness_months,
                    request_delay=request_delay,
                    retries=scout_retries,
                    timeout=scout_timeout,
                ),
            )
            scout_results.append(scout_result)
            if scout_result["errors"] and not scout_result["eligible_count"]:
                db.update_workflow_state(connection, cycle_id, "failed")
                break

            candidates = db.eligible_candidates_for_cycle(connection, cycle_id)
            curator_result = CuratorAgent().run(
                connection,
                workflow_cycle_id=cycle_id,
                candidates=candidates,
                profile_version=profile_version,
                scout_attempt_count=attempt,
                config=CuratorConfig(
                    max_recommendations=max_recommendations,
                    min_quality_score=min_quality_score,
                    max_scout_attempts=max_scout_attempts,
                ),
            )
            if not curator_result["requested_rescout"]:
                break

        if curator_result is not None:
            db.update_workflow_state(connection, cycle_id, "awaiting_manual_discussion")
            recommendations = curator_result.get("recommendations") or []
            for index, recommendation in enumerate(recommendations, 1):
                card = extract_recommendation(
                    connection,
                    recommendation,
                    index=index,
                    pdf_dir=pdf_dir,
                    model=model,
                    ollama_url=ollama_url,
                    max_chars=max_chars,
                    limit_chunks=limit_chunks,
                    timeout=timeout,
                    workers=workers,
                )
                cards.append(card)

        cycle = db.get_workflow_cycle(connection, cycle_id)

    return {
        "workflow_cycle_id": cycle_id,
        "cycle": cycle,
        "profile_version_id": profile_version_id,
        "scout_results": scout_results,
        "curator": curator_result,
        "model": model,
        "max_chars": max_chars,
        "limit_chunks": limit_chunks,
        "workers": workers,
        "cards": cards,
    }


def extract_recommendation(
    connection,
    recommendation: dict[str, Any],
    *,
    index: int,
    pdf_dir: Path,
    model: str,
    ollama_url: str,
    max_chars: int,
    limit_chunks: int,
    timeout: int,
    workers: int,
) -> dict[str, Any]:
    pdf_path = download_pdf_for_recommendation(recommendation, pdf_dir)
    if pdf_path:
        db.insert_artifact(
            connection,
            recommendation["paper_id"],
            artifact_type="pdf",
            path=pdf_path,
            metadata={"pdf_url": recommendation.get("pdf_url")},
        )
    else:
        return card_from_recommendation(recommendation, index=index, error="PDF was not downloaded")

    output_path = output_path_for(pdf_path, model)
    print(f"extracting recommended paper {index}: {pdf_path}")
    try:
        extraction = extract_paper(
            pdf_path,
            model=model,
            ollama_url=ollama_url,
            max_chars=max_chars,
            limit_chunks=limit_chunks,
            timeout=timeout,
            workers=workers,
        )
    except RuntimeError as error:
        return card_from_recommendation(recommendation, index=index, pdf_path=pdf_path, error=str(error))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(extraction, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote extraction: {output_path}")
    db.insert_artifact(
        connection,
        recommendation["paper_id"],
        artifact_type="triage_summary",
        path=output_path,
        model=model,
        metadata={
            "merge_strategy": extraction.get("merge_strategy"),
            "chunk_count": extraction.get("chunk_count"),
            "source_path": extraction.get("source_path"),
        },
    )
    return card_from_recommendation(
        recommendation,
        index=index,
        pdf_path=pdf_path,
        extraction=extraction,
        output_path=output_path,
    )


def download_pdf_for_recommendation(recommendation: dict[str, Any], pdf_dir: Path, timeout: int = 60) -> Path | None:
    pdf_url = recommendation.get("pdf_url")
    if not pdf_url:
        return None
    source = recommendation.get("source") or "unknown"
    source_id = recommendation.get("source_id") or recommendation.get("canonical_key") or recommendation.get("title")
    source_dir = pdf_dir / source
    source_dir.mkdir(parents=True, exist_ok=True)
    destination = source_dir / (safe_filename(str(source_id)) + ".pdf")
    if destination.exists() and destination.stat().st_size > 0:
        return destination
    request = urllib.request.Request(pdf_url, headers={"User-Agent": "paper-agent/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read()
    except OSError:
        return None
    if not data.startswith(b"%PDF"):
        return None
    destination.write_bytes(data)
    return destination


def card_from_recommendation(
    recommendation: dict[str, Any],
    *,
    index: int,
    pdf_path: Path | None = None,
    extraction: dict[str, Any] | None = None,
    output_path: Path | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    merged = extraction.get("merged", {}) if extraction else {}
    return {
        "recommendation_order": index,
        "paper_id": recommendation.get("paper_id"),
        "title": recommendation.get("title"),
        "url": recommendation.get("url"),
        "pdf_path": str(pdf_path) if pdf_path else None,
        "summary_path": str(output_path) if output_path else None,
        "score": recommendation.get("score"),
        "published": recommendation.get("published"),
        "paper_date": merged.get("paper_date") or recommendation.get("published"),
        "research_problem": merged.get("research_problem"),
        "why_it_matters": merged.get("why_it_matters"),
        "approach": merged.get("approach"),
        "curator_rationale": recommendation.get("rationale"),
        "merge_strategy": extraction.get("merge_strategy") if extraction else None,
        "chunk_count": extraction.get("chunk_count") if extraction else None,
        "error": error,
    }
