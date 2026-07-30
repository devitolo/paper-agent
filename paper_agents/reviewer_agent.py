from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from paper_agents import db
from paper_agents.local_extract import DEFAULT_MODEL, DEFAULT_OLLAMA_URL, extract_paper, output_path_for
from paper_agents.scout import DEFAULT_PDF_DIR, safe_filename


DEFAULT_REVIEWER_MAX_CHARS = 7000
DEFAULT_REVIEWER_LIMIT_CHUNKS = 0
DEFAULT_REVIEWER_WORKERS = 2
DEFAULT_REVIEWER_TIMEOUT = 600


@dataclass(frozen=True)
class ReviewerConfig:
    pdf_dir: Path = DEFAULT_PDF_DIR
    model: str = DEFAULT_MODEL
    ollama_url: str = DEFAULT_OLLAMA_URL
    max_chars: int = DEFAULT_REVIEWER_MAX_CHARS
    limit_chunks: int = DEFAULT_REVIEWER_LIMIT_CHUNKS
    timeout: int = DEFAULT_REVIEWER_TIMEOUT
    workers: int = DEFAULT_REVIEWER_WORKERS


class ReviewerAgent:
    """Downloads recommended papers and creates local triage summary artifacts."""

    def run(
        self,
        connection,
        *,
        recommendations: list[dict[str, Any]],
        config: ReviewerConfig,
    ) -> dict[str, Any]:
        cards = [
            self.review_recommendation(
                connection,
                recommendation,
                index=index,
                config=config,
            )
            for index, recommendation in enumerate(recommendations, 1)
        ]
        return {"cards": cards, "reviewed_count": len(cards)}

    def review_recommendation(
        self,
        connection,
        recommendation: dict[str, Any],
        *,
        index: int,
        config: ReviewerConfig,
    ) -> dict[str, Any]:
        pdf_path = download_pdf_for_recommendation(recommendation, config.pdf_dir)
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

        output_path = output_path_for(pdf_path, config.model)
        print(f"extracting recommended paper {index}: {pdf_path}")
        try:
            extraction = extract_paper(
                pdf_path,
                model=config.model,
                ollama_url=config.ollama_url,
                max_chars=config.max_chars,
                limit_chunks=config.limit_chunks,
                timeout=config.timeout,
                workers=config.workers,
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
            model=config.model,
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
