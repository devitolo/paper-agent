from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from paper_agents import db
from paper_agents.scout import (
    DEFAULT_ARXIV_REQUEST_DELAY,
    DEFAULT_ARXIV_RETRIES,
    DEFAULT_ARXIV_TIMEOUT,
    DEFAULT_FETCH_LIMIT,
    DEFAULT_FRESHNESS_MONTHS,
    DEFAULT_SCOUT_TOPICS,
    ArxivSource,
    PaperSource,
    dedupe_candidates,
)

DEFAULT_TARGET_CANDIDATES = 20


@dataclass(frozen=True)
class ScoutConfig:
    topics: list[str]
    target_candidates: int = DEFAULT_TARGET_CANDIDATES
    max_candidates: int = DEFAULT_FETCH_LIMIT
    freshness_months: int = DEFAULT_FRESHNESS_MONTHS
    request_delay: float = DEFAULT_ARXIV_REQUEST_DELAY
    retries: int = DEFAULT_ARXIV_RETRIES
    timeout: int = DEFAULT_ARXIV_TIMEOUT


class ScoutAgent:
    """Retrieves source candidates and records a candidate pool without preference scoring."""

    def __init__(self, source: PaperSource | None = None):
        self.source = source

    def run(
        self,
        connection,
        *,
        workflow_cycle_id: int,
        attempt_number: int,
        config: ScoutConfig,
    ) -> dict[str, Any]:
        source = self.source or ArxivSource(
            request_delay=config.request_delay,
            retries=config.retries,
            timeout=config.timeout,
        )
        guidance = db.active_scouting_guidance(connection)
        db.update_workflow_state(connection, workflow_cycle_id, "scouting")
        db.increment_scout_attempts(connection, workflow_cycle_id)
        scout_run_id = db.insert_scout_run(
            connection,
            workflow_cycle_id=workflow_cycle_id,
            attempt_number=attempt_number,
            source=source.name,
            target_candidates=config.target_candidates,
            max_candidates=config.max_candidates,
            freshness_months=config.freshness_months,
            topics=config.topics,
            guidance_id=guidance["id"] if guidance else None,
            diagnostics={"guidance_text": guidance.get("guidance_text") if guidance else None},
        )

        warnings: list[str] = []
        errors: list[str] = []
        candidates = []
        source_diagnostics: dict[str, Any] = {}
        try:
            candidates = dedupe_candidates(
                source.fetch(
                    config.topics or DEFAULT_SCOUT_TOPICS,
                    max_results=config.max_candidates,
                    freshness_months=config.freshness_months,
                )
            )
            source_diagnostics = dict(getattr(source, "last_diagnostics", {}) or {})
        except Exception as error:  # source adapters normalize most errors, but keep runs recoverable.
            errors.append(str(error))

        stored: list[dict[str, Any]] = []
        for index, candidate in enumerate(candidates, 1):
            candidate_dict = sanitize_candidate(candidate.as_dict())
            paper_id, is_new = db.upsert_paper(connection, candidate_dict)
            seen_in_current_cycle = db.paper_has_scout_candidate_in_cycle(
                connection,
                paper_id=paper_id,
                workflow_cycle_id=workflow_cycle_id,
                before_scout_run_id=scout_run_id,
            )
            excluded = not is_new and not seen_in_current_cycle
            exclusion_reason = "previously_discovered" if excluded else None
            scout_candidate_id = db.insert_scout_candidate(
                connection,
                scout_run_id=scout_run_id,
                paper_id=paper_id,
                retrieval_order=index,
                is_new=is_new,
                excluded=excluded,
                exclusion_reason=exclusion_reason,
                source_query=(candidate.metadata or {}).get("query_topic"),
                source_diagnostics={"primary_category": candidate.primary_category},
            )
            stored.append(
                {
                    **candidate_dict,
                    "paper_id": paper_id,
                    "scout_candidate_id": scout_candidate_id,
                    "is_new": is_new,
                    "excluded": excluded,
                    "exclusion_reason": exclusion_reason,
                    "retrieval_order": index,
                }
            )

        db.complete_scout_run(
            connection,
            scout_run_id,
            diagnostics={
                "fetched_count": len(candidates),
                "stored_count": len(stored),
                "source_diagnostics": source_diagnostics,
            },
            warnings=warnings,
            errors=errors,
        )
        db.update_workflow_state(connection, workflow_cycle_id, "scout_complete")
        return {
            "scout_run_id": scout_run_id,
            "source": source.name,
            "attempt_number": attempt_number,
            "fetched_count": len(candidates),
            "stored_count": len(stored),
            "eligible_count": len([candidate for candidate in stored if not candidate["excluded"]]),
            "warnings": warnings,
            "errors": errors,
            "candidates": stored,
        }


def sanitize_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    clean = dict(candidate)
    clean.pop("score", None)
    clean.pop("matched_keywords", None)
    clean.pop("ranking_reason", None)
    clean.pop("selected", None)
    return clean
