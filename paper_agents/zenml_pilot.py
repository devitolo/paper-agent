from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from paper_agents.db import (
    DEFAULT_DB_PATH,
    complete_scout_run,
    connect_db,
    create_curator_run,
    create_workflow_cycle,
    init_db,
    insert_artifact,
    insert_curator_evaluation,
    insert_recommendation,
    insert_scout_candidate,
    insert_scout_run,
    seen_source_ids,
    update_workflow_state,
    upsert_paper,
)

ZENML_SOURCE = "zenml"
ZENML_DATASET_URL = "https://datasets-server.huggingface.co/rows"
ZENML_DATASET_PARAMS = {
    "dataset": "zenml/llmops-database",
    "config": "default",
    "split": "train",
}
PILOT_MODEL = "zenml-pilot-summary-v1"
PILOT_TOPICS = [
    "internal AI assistants and knowledge access",
    "shared AI platforms and enablement",
    "employee-built AI tools",
    "AI-assisted engineering workflows",
]

# Already reviewed examples from the user's current queue. The pilot should show fresh items.
SEED_EXCLUDED_URLS = {
    "https://slack.engineering/empowering-engineers-with-ai/",
    "https://shopify.engineering/quick",
    "https://www.uber.com/us/en/blog/ureview/",
    "https://www.engineering.atspotify.com/2025/11/spotifys-background-coding-agent-part-1",
    "https://www.uber.com/au/en/blog/first-pass-prd/",
}

HIGH_VALUE_TERMS = {
    "assistant": 12,
    "assistants": 12,
    "knowledge": 12,
    "internal": 10,
    "employee": 10,
    "employees": 10,
    "platform": 10,
    "enablement": 10,
    "self-service": 10,
    "agent": 8,
    "agents": 8,
    "workflow": 8,
    "workflows": 8,
    "adoption": 8,
    "productivity": 6,
    "copilot": 6,
    "mcp": 6,
}
ENGINEERING_TERMS = {
    "engineering": 10,
    "developer": 10,
    "developers": 10,
    "code": 8,
    "coding": 8,
    "testing": 8,
    "review": 6,
    "migration": 6,
    "maintenance": 6,
    "feature flag": 6,
    "pull request": 6,
    "llmops": 5,
}
DEEMPHASIS_TERMS = {
    "root cause": 12,
    "incident": 10,
    "log analysis": 8,
    "observability": 5,
}


def fetch_zenml_rows(*, fetch_limit: int = 200, timeout: int = 30) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    page_size = 100
    for offset in range(0, max(fetch_limit, 0), page_size):
        length = min(page_size, fetch_limit - offset)
        if length <= 0:
            break
        params = dict(ZENML_DATASET_PARAMS)
        params.update({"offset": str(offset), "length": str(length)})
        url = f"{ZENML_DATASET_URL}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers={"User-Agent": "project-paper-zenml-pilot/1.0"})
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310: fixed public dataset URL
            payload = json.loads(response.read().decode("utf-8"))
        rows = payload.get("rows") if isinstance(payload, dict) else []
        if not rows:
            break
        for item in rows:
            row = item.get("row") if isinstance(item, dict) else None
            if isinstance(row, dict):
                row = {**row, "_row_idx": item.get("row_idx")}
                normalized.append(row)
    return normalized


def select_zenml_candidates(rows: list[dict[str, Any]], *, keep: int = 5, excluded_urls: set[str] | None = None) -> list[dict[str, Any]]:
    excluded_urls = excluded_urls or set()
    scored: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        candidate = row_to_candidate(row)
        if not candidate:
            continue
        if candidate["source_id"] in excluded_urls or is_non_article_url(candidate["source_id"]):
            continue
        score, signals = score_candidate(candidate)
        if score <= 0:
            continue
        candidate["metadata"]["pilot_score"] = score
        candidate["metadata"]["pilot_signals"] = signals
        scored.append((score, candidate))
    scored.sort(key=lambda item: (-item[0], item[1]["title"].lower()))
    return [candidate for _, candidate in scored[:keep]]


def is_non_article_url(url: str) -> bool:
    lowered = url.lower()
    return "youtube.com/" in lowered or "youtu.be/" in lowered


def row_to_candidate(row: dict[str, Any]) -> dict[str, Any] | None:
    title = str(row.get("title") or "").strip()
    source_url = str(row.get("source_url") or "").strip()
    short_summary = str(row.get("short_summary") or "").strip()
    full_summary = str(row.get("full_summary") or "").strip()
    if not title or not source_url or not (short_summary or full_summary):
        return None
    created_at = str(row.get("created_at") or "").strip()
    year = str(row.get("year") or "").strip()
    published = created_at[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", created_at) else (f"{year}-01-01" if year.isdigit() else None)
    tags = tag_list(row)
    return {
        "source": ZENML_SOURCE,
        "source_id": source_url,
        "title": title,
        "abstract": short_summary or full_summary,
        "published": published,
        "updated": created_at[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", created_at) else None,
        "authors": [str(row.get("company") or "ZenML LLMOps Database")],
        "categories": tags,
        "url": source_url,
        "pdf_url": None,
        "metadata": {
            "discovery_source": "ZenML LLMOps Database",
            "source_url": source_url,
            "webflow_url": row.get("webflow_url"),
            "company": row.get("company"),
            "industry": row.get("industry"),
            "year": row.get("year"),
            "tags": tags,
            "short_summary": short_summary,
            "full_summary": full_summary,
            "zenml_row_idx": row.get("_row_idx"),
        },
    }


def tag_list(row: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    for key in ("application_tags", "tools_tags", "extra_tags", "techniques_tags"):
        value = row.get(key)
        if isinstance(value, list):
            tags.extend(str(item) for item in value if item)
        elif isinstance(value, str) and value.strip():
            tags.extend(part.strip() for part in value.split(",") if part.strip())
    return sorted(dict.fromkeys(tags))


def score_candidate(candidate: dict[str, Any]) -> tuple[float, list[str]]:
    metadata = candidate.get("metadata") or {}
    text = " ".join(
        str(part or "")
        for part in (
            candidate.get("title"),
            candidate.get("abstract"),
            metadata.get("company"),
            metadata.get("industry"),
            " ".join(metadata.get("tags") or []),
            metadata.get("full_summary"),
        )
    ).lower()
    score = 0.0
    signals: list[str] = []
    for term, weight in HIGH_VALUE_TERMS.items():
        if term in text:
            score += weight
            signals.append(term)
    for term, weight in ENGINEERING_TERMS.items():
        if term in text:
            score += weight
            signals.append(term)
    if "ai" in text or "llm" in text or "genai" in text:
        score += 8
        signals.append("ai/llm")
    if metadata.get("company"):
        score += 4
        signals.append("company case study")
    if not any(term in signals for term in ("assistant", "assistants", "knowledge", "internal", "employee", "platform", "enablement", "self-service")):
        for term, weight in DEEMPHASIS_TERMS.items():
            if term in text:
                score -= weight
    return max(score, 0.0), sorted(dict.fromkeys(signals))


def import_zenml_pilot(
    *,
    db_path: Path = DEFAULT_DB_PATH,
    fetch_limit: int = 200,
    keep: int = 5,
    rows: list[dict[str, Any]] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    init_db(db_path)
    existing = seen_source_ids(db_path, source=ZENML_SOURCE)
    if len(existing - SEED_EXCLUDED_URLS) >= keep and rows is None and not dry_run:
        return {
            "status": "already_imported",
            "source": ZENML_SOURCE,
            "existing_count": len(existing - SEED_EXCLUDED_URLS),
            "imported_count": 0,
            "items": [],
        }

    fetched_rows = rows if rows is not None else fetch_zenml_rows(fetch_limit=fetch_limit)
    selected = select_zenml_candidates(
        fetched_rows,
        keep=keep,
        excluded_urls=set(existing) | SEED_EXCLUDED_URLS,
    )
    if dry_run:
        return {
            "status": "dry_run",
            "source": ZENML_SOURCE,
            "fetched_count": len(fetched_rows),
            "selected_count": len(selected),
            "items": summarize_items(selected),
        }

    with connect_db(db_path) as connection:
        cycle_id = create_workflow_cycle(
            connection,
            mode="zenml_pilot",
            max_scout_attempts=1,
            metadata={"source": ZENML_SOURCE, "pilot": True, "academic_relevance_model": False},
        )
        update_workflow_state(connection, cycle_id, "scouting")
        scout_run_id = insert_scout_run(
            connection,
            workflow_cycle_id=cycle_id,
            attempt_number=1,
            source=ZENML_SOURCE,
            target_candidates=keep,
            max_candidates=fetch_limit,
            freshness_months=0,
            topics=PILOT_TOPICS,
            guidance_id=None,
            diagnostics={"dataset": "zenml/llmops-database", "fetched_count": len(fetched_rows)},
        )
        imported: list[dict[str, Any]] = []
        scout_candidate_ids: dict[int, int] = {}
        for order, candidate in enumerate(selected, start=1):
            paper_id, is_new = upsert_paper(connection, candidate)
            scout_candidate_ids[paper_id] = insert_scout_candidate(
                connection,
                scout_run_id=scout_run_id,
                paper_id=paper_id,
                retrieval_order=order,
                is_new=is_new,
                source_query="ZenML LLMOps Database pilot",
                source_diagnostics={"pilot_score": candidate["metadata"].get("pilot_score"), "signals": candidate["metadata"].get("pilot_signals")},
            )
            write_triage_artifact(connection, db_path, paper_id, candidate)
            imported.append({"paper_id": paper_id, **candidate_summary(candidate)})
        complete_scout_run(connection, scout_run_id, diagnostics={"selected_count": len(imported), "imported_count": len(imported)})

        update_workflow_state(connection, cycle_id, "curating")
        curator_run_id = create_curator_run(
            connection,
            workflow_cycle_id=cycle_id,
            profile_version_id=None,
            scout_attempt_count=1,
            max_scout_attempts=1,
            min_quality_score=0.0,
            max_recommendations=keep,
            model=PILOT_MODEL,
            metadata={"pilot": True, "source": ZENML_SOURCE, "note": "No academic relevance model was used."},
        )
        for order, item in enumerate(imported, start=1):
            paper_id = int(item["paper_id"])
            rationale = "ZenML pilot: selected as a practical AI engineering case study. Original article URL is the evidence link."
            signals = item.get("signals") or []
            insert_curator_evaluation(
                connection,
                curator_run_id=curator_run_id,
                paper_id=paper_id,
                scout_candidate_id=scout_candidate_ids.get(paper_id),
                score=80.0 - (order - 1),
                rationale=rationale,
                matched_signals=list(signals),
                quality_threshold_met=True,
            )
            insert_recommendation(
                connection,
                curator_run_id=curator_run_id,
                paper_id=paper_id,
                recommendation_order=order,
                rationale=rationale,
            )
        update_workflow_state(connection, cycle_id, "awaiting_manual_discussion")

    return {
        "status": "imported",
        "source": ZENML_SOURCE,
        "fetched_count": len(fetched_rows),
        "imported_count": len(imported),
        "workflow_cycle_id": cycle_id,
        "items": imported,
    }


def write_triage_artifact(connection: Any, db_path: Path, paper_id: int, candidate: dict[str, Any]) -> int:
    output_dir = db_path.parent / "zenml-pilot"
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{slugify(candidate['title'])}.summary.json"
    summary = build_summary(candidate)
    path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return insert_artifact(
        connection,
        paper_id,
        artifact_type="triage_summary",
        path=path,
        model=PILOT_MODEL,
        metadata={
            "abstract_only": True,
            "source_type": "zenml_summary",
            "full_text_available": False,
            "discovery_source": "ZenML LLMOps Database",
            "source_url": candidate.get("url"),
        },
    )


def build_summary(candidate: dict[str, Any]) -> dict[str, Any]:
    metadata = candidate.get("metadata") or {}
    company = metadata.get("company") or "the company"
    title = candidate.get("title") or "Untitled ZenML item"
    short_summary = metadata.get("short_summary") or candidate.get("abstract") or "ZenML summary was unavailable."
    full_summary = metadata.get("full_summary") or short_summary
    approach = first_sentence(full_summary) or short_summary
    return {
        "merged": {
            "research_problem": f"{company} case study: {title}",
            "why_it_matters": short_summary,
            "approach": approach,
        },
        "abstract_only": True,
        "source_type": "zenml_summary",
        "full_text_available": False,
        "discovery_source": "ZenML LLMOps Database",
        "source_url": candidate.get("url"),
        "warning": "Generated from ZenML summary metadata; original article text was not ingested.",
    }


def first_sentence(text: str) -> str:
    match = re.search(r"(.{30,}?[.!?])\s", text.strip())
    return match.group(1).strip() if match else text.strip()[:240]


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:80] or "zenml-item"


def summarize_items(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [candidate_summary(candidate) for candidate in candidates]


def candidate_summary(candidate: dict[str, Any]) -> dict[str, Any]:
    metadata = candidate.get("metadata") or {}
    return {
        "title": candidate.get("title"),
        "company": metadata.get("company"),
        "industry": metadata.get("industry"),
        "url": candidate.get("url"),
        "score": metadata.get("pilot_score"),
        "signals": metadata.get("pilot_signals") or [],
    }
