from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from paper_agents.local_extract import prepare_text_source
from paper_agents.openai_helpers import call_openai_text

DEFAULT_REVIEW_DIR = Path("data/reviews")
DEFAULT_REVIEW_MAX_CHARS = 45000


def review_summary(
    summary_path: Path,
    *,
    output_dir: Path = DEFAULT_REVIEW_DIR,
    max_chars: int = DEFAULT_REVIEW_MAX_CHARS,
    timeout: int = 240,
) -> dict[str, Any]:
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    pdf_path = pdf_path_from_summary(summary)
    if not pdf_path:
        raise RuntimeError("Could not find source PDF path in summary JSON.")
    if not pdf_path.exists():
        raise RuntimeError(f"Source PDF does not exist: {pdf_path}")

    text_path, cleanup_dir = prepare_text_source(pdf_path)
    try:
        paper_text = text_path.read_text(encoding="utf-8", errors="ignore")[:max_chars]
    finally:
        if cleanup_dir is not None:
            cleanup_dir.cleanup()

    metadata = summary_metadata(summary, summary_path, pdf_path)
    review_markdown = call_openai_text(
        build_review_system_prompt(),
        build_review_user_content(metadata, paper_text),
        timeout=timeout,
    )

    output_path = review_output_path(summary_path, metadata, output_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(review_markdown.rstrip() + "\n", encoding="utf-8")
    return {
        "summary_path": str(summary_path),
        "pdf_path": str(pdf_path),
        "review_path": str(output_path),
        "title": metadata.get("title"),
        "paper_date": metadata.get("paper_date"),
    }


def pdf_path_from_summary(summary: dict[str, Any]) -> Path | None:
    source = summary.get("source")
    if source:
        return Path(source)
    return None


def summary_metadata(summary: dict[str, Any], summary_path: Path, pdf_path: Path) -> dict[str, Any]:
    merged = summary.get("merged", {}) if isinstance(summary.get("merged"), dict) else {}
    source_metadata = (
        summary.get("source_metadata", {}) if isinstance(summary.get("source_metadata"), dict) else {}
    )
    return {
        "summary_path": str(summary_path),
        "pdf_path": str(pdf_path),
        "title": source_metadata.get("title") or pdf_path.stem,
        "paper_date": merged.get("paper_date") or source_metadata.get("paper_date"),
        "url": source_metadata.get("url"),
        "arxiv_id": source_metadata.get("arxiv_id"),
        "triage": merged,
    }


def build_review_system_prompt() -> str:
    return """
You write careful research-paper briefings for a technical listener who usually does not read the PDF directly.
Use the paper text and triage metadata only. Be concrete, grounded, and practical. Prefer clear spoken-language prose over academic stiffness.
Return Markdown. Do not invent results or limitations that are not supported by the provided text.
""".strip()


def build_review_user_content(metadata: dict[str, Any], paper_text: str) -> str:
    return f"""
Create a listening-friendly section-by-section review of this paper.

Metadata and local triage:
{json.dumps(metadata, indent=2, ensure_ascii=False)}

Required Markdown structure:
# Paper Review

## Listening Overview
A concise 2-4 paragraph overview suitable to listen to while walking or driving.

## Why This Paper Might Matter
Explain the practical relevance for AI, SRE, operations, engineering workflows, or developer productivity.

## Section-By-Section Summary
Summarize the paper section by section. Use the section names from the paper when visible. If sections are not clear, group the content into logical sections and say so.

## Method Or Architecture
Explain the proposed method, system, architecture, or experiment in plain language.

## Evidence And Results
Summarize the evidence, evaluation setup, datasets, benchmarks, or case studies. If evidence is not visible in the provided text, say that.

## Practical Takeaways
List concrete ideas, patterns, or cautions that might be useful later.

## Limitations And Caveats
State limitations the paper mentions or limitations that are directly apparent from the provided text. Do not speculate beyond the text.

## Questions To Ask
List questions worth asking before relying on this paper.

## Audio Notes
Rewrite the most important points as a short spoken script. Use natural phrasing and avoid dense citations.

Paper text excerpt, truncated if necessary:
{paper_text}
""".strip()


def review_output_path(summary_path: Path, metadata: dict[str, Any], output_dir: Path) -> Path:
    if metadata.get("arxiv_id"):
        return output_dir / "arxiv" / f"{metadata['arxiv_id']}.review.md"
    return output_dir / "manual" / f"{safe_slug(summary_path.stem)}.review.md"


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._")
    return slug[:120] if slug else "paper"
