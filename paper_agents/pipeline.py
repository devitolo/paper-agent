from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from paper_agents.db import DEFAULT_DB_PATH, connect_db, insert_artifact, record_scout_output
from paper_agents.local_extract import (
    DEFAULT_MODEL,
    DEFAULT_OLLAMA_URL,
    extract_paper,
    output_path_for,
)
from paper_agents.scout import (
    DEFAULT_ARXIV_REQUEST_DELAY,
    DEFAULT_ARXIV_RETRIES,
    DEFAULT_ARXIV_TIMEOUT,
    DEFAULT_FETCH_LIMIT,
    DEFAULT_FRESHNESS_MONTHS,
    DEFAULT_KEEP_LIMIT,
    DEFAULT_PDF_DIR,
    DEFAULT_SCOUT_DIR,
    DEFAULT_SCOUT_TOPICS,
    run_daily_scout,
)

DEFAULT_PIPELINE_MAX_CHARS = 7000
DEFAULT_PIPELINE_LIMIT_CHUNKS = 0
DEFAULT_PIPELINE_WORKERS = 2
DEFAULT_PIPELINE_TIMEOUT = 600


def run_daily_pipeline(
    *,
    topics: list[str] | None = None,
    freshness_months: int = DEFAULT_FRESHNESS_MONTHS,
    fetch_limit: int = DEFAULT_FETCH_LIMIT,
    keep_limit: int = DEFAULT_KEEP_LIMIT,
    scout_dir: Path = DEFAULT_SCOUT_DIR,
    pdf_dir: Path = DEFAULT_PDF_DIR,
    model: str = DEFAULT_MODEL,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    max_chars: int = DEFAULT_PIPELINE_MAX_CHARS,
    limit_chunks: int = DEFAULT_PIPELINE_LIMIT_CHUNKS,
    timeout: int = DEFAULT_PIPELINE_TIMEOUT,
    workers: int = DEFAULT_PIPELINE_WORKERS,
    db_path: Path | None = DEFAULT_DB_PATH,
    mode: str = "full",
    request_delay: float = DEFAULT_ARXIV_REQUEST_DELAY,
    scout_retries: int = DEFAULT_ARXIV_RETRIES,
    scout_timeout: int = DEFAULT_ARXIV_TIMEOUT,
) -> dict[str, Any]:
    """Run Scout, download selected PDFs, and extract local triage cards."""
    scout_output = run_daily_scout(
        topics=topics or DEFAULT_SCOUT_TOPICS,
        freshness_months=freshness_months,
        fetch_limit=fetch_limit,
        keep_limit=keep_limit,
        scout_dir=scout_dir,
        pdf_dir=pdf_dir,
        download_pdfs=True,
        request_delay=request_delay,
        retries=scout_retries,
        timeout=scout_timeout,
    )

    registry: dict[str, Any] | None = None
    paper_ids: dict[str, int] = {}
    topics_for_run = topics or DEFAULT_SCOUT_TOPICS
    if db_path is not None:
        registry = record_scout_output(
            db_path,
            scout_output,
            topics=topics_for_run,
            fetch_limit=fetch_limit,
            keep_limit=keep_limit,
            mode=mode,
        )
        paper_ids = registry.get("paper_ids", {})
        print(f"recorded scout run in SQLite: {db_path} (run_id={registry['run_id']})")

    cards: list[dict[str, Any]] = []
    selected = scout_output.get("selected", [])
    for index, candidate in enumerate(selected, 1):
        pdf_path = candidate.get("pdf_path")
        if not pdf_path:
            cards.append(card_from_candidate(candidate, error="PDF was not downloaded"))
            continue

        source_path = Path(pdf_path)
        output_path = output_path_for(source_path, model)
        print(f"extracting selected paper {index}/{len(selected)}: {source_path}")
        try:
            extraction = extract_paper(
                source_path,
                model=model,
                ollama_url=ollama_url,
                max_chars=max_chars,
                limit_chunks=limit_chunks,
                timeout=timeout,
                workers=workers,
            )
        except RuntimeError as error:
            cards.append(card_from_candidate(candidate, error=str(error)))
            continue

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(extraction, indent=2, ensure_ascii=False) + "\n")
        print(f"wrote extraction: {output_path}")
        if db_path is not None:
            register_summary_artifact(
                db_path,
                candidate,
                paper_ids,
                output_path,
                model=model,
                extraction=extraction,
            )
        cards.append(card_from_candidate(candidate, extraction=extraction, output_path=output_path))

    return {
        "scout": scout_output,
        "model": model,
        "max_chars": max_chars,
        "limit_chunks": limit_chunks,
        "workers": workers,
        "registry": registry,
        "cards": cards,
    }


def card_from_candidate(
    candidate: dict[str, Any],
    *,
    extraction: dict[str, Any] | None = None,
    output_path: Path | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    merged = extraction.get("merged", {}) if extraction else {}
    return {
        "title": candidate.get("title"),
        "url": candidate.get("url"),
        "pdf_path": candidate.get("pdf_path"),
        "summary_path": str(output_path) if output_path else None,
        "score": candidate.get("score"),
        "published": candidate.get("published"),
        "paper_date": merged.get("paper_date") or candidate.get("published"),
        "research_problem": merged.get("research_problem"),
        "why_it_matters": merged.get("why_it_matters"),
        "approach": merged.get("approach"),
        "merge_strategy": extraction.get("merge_strategy") if extraction else None,
        "chunk_count": extraction.get("chunk_count") if extraction else None,
        "error": error,
    }


def register_summary_artifact(
    db_path: Path,
    candidate: dict[str, Any],
    paper_ids: dict[str, int],
    output_path: Path,
    *,
    model: str,
    extraction: dict[str, Any],
) -> None:
    paper_id = paper_ids.get(candidate_registry_key(candidate))
    if paper_id is None:
        return

    metadata = {
        "merge_strategy": extraction.get("merge_strategy"),
        "chunk_count": extraction.get("chunk_count"),
        "source_path": extraction.get("source_path"),
    }
    with connect_db(db_path) as connection:
        insert_artifact(
            connection,
            paper_id,
            artifact_type="triage_summary",
            path=output_path,
            model=model,
            metadata=metadata,
        )


def candidate_registry_key(candidate: dict[str, Any]) -> str:
    source = str(candidate.get("source") or "unknown")
    source_id = str(candidate.get("source_id") or candidate.get("url") or candidate.get("title") or "unknown")
    return f"{source}:{source_id}"
