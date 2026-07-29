from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from paper_agents.local_extract import (
    DEFAULT_MODEL,
    DEFAULT_OLLAMA_URL,
    extract_paper,
    output_path_for,
)
from paper_agents.scout import (
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
    )

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
        cards.append(card_from_candidate(candidate, extraction=extraction, output_path=output_path))

    return {
        "scout": scout_output,
        "model": model,
        "max_chars": max_chars,
        "limit_chunks": limit_chunks,
        "workers": workers,
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
