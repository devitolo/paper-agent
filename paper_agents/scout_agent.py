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
from paper_agents.scout_guidance import (
    apply_guidance_to_candidates,
    guidance_summary,
    guidance_text,
    load_scout_guidance,
    topics_with_guidance,
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
        active_guidance = db.active_scouting_guidance(connection)
        scout_guidance = load_scout_guidance(connection)
        guided_topics = topics_with_guidance(config.topics or DEFAULT_SCOUT_TOPICS, scout_guidance)
        guidance_id = db.create_scouting_guidance(
            connection,
            curator_run_id=None,
            guidance_text=guidance_text(scout_guidance),
            metadata={
                "source": "feedback_profile",
                "guidance": scout_guidance.as_dict(),
                "base_topics": config.topics,
                "guided_topics": guided_topics,
                "active_curator_guidance_id": active_guidance["id"] if active_guidance else None,
                "active_curator_guidance_text": active_guidance.get("guidance_text") if active_guidance else None,
            },
            active=False,
        )
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
            topics=guided_topics,
            guidance_id=guidance_id,
            diagnostics={
                "scout_guidance": guidance_summary(scout_guidance),
                "base_topics": config.topics,
                "active_curator_guidance_text": active_guidance.get("guidance_text") if active_guidance else None,
            },
        )

        warnings: list[str] = []
        errors: list[str] = []
        candidates = []
        guided_candidates = []
        source_diagnostics: dict[str, Any] = {}
        try:
            candidates = dedupe_candidates(
                source.fetch(
                    guided_topics,
                    max_results=config.max_candidates,
                    freshness_months=config.freshness_months,
                )
            )
            guided_candidates = apply_guidance_to_candidates(candidates, scout_guidance)
            source_diagnostics = dict(getattr(source, "last_diagnostics", {}) or {})
        except Exception as error:  # source adapters normalize most errors, but keep runs recoverable.
            errors.append(str(error))

        stored: list[dict[str, Any]] = []
        for index, (candidate, guidance_diagnostics) in enumerate(guided_candidates, 1):
            candidate_dict = sanitize_candidate(candidate.as_dict())
            paper_id, is_new = db.upsert_paper(connection, candidate_dict)
            seen_in_current_cycle = db.paper_has_scout_candidate_in_cycle(
                connection,
                paper_id=paper_id,
                workflow_cycle_id=workflow_cycle_id,
                before_scout_run_id=scout_run_id,
            )
            feedback_excluded = bool(guidance_diagnostics.get("feedback_guidance_excluded"))
            already_recommended = db.paper_has_prior_recommendation(
                connection,
                paper_id=paper_id,
                before_scout_run_id=scout_run_id,
            )
            excluded = feedback_excluded or already_recommended or (not is_new and not seen_in_current_cycle)
            exclusion_reason = None
            if feedback_excluded:
                exclusion_reason = "feedback_avoid_terms"
            elif already_recommended:
                exclusion_reason = "already_recommended"
            elif excluded:
                exclusion_reason = "previously_discovered"
            scout_candidate_id = db.insert_scout_candidate(
                connection,
                scout_run_id=scout_run_id,
                paper_id=paper_id,
                retrieval_order=index,
                is_new=is_new,
                excluded=excluded,
                exclusion_reason=exclusion_reason,
                source_query=(candidate.metadata or {}).get("query_topic"),
                source_diagnostics={
                    "primary_category": candidate.primary_category,
                    "feedback_guidance": guidance_diagnostics,
                },
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
                "scout_guidance": guidance_summary(scout_guidance),
                "base_topics": config.topics,
                "guided_topics": guided_topics,
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
            "guidance": guidance_summary(scout_guidance),
            "base_topics": config.topics,
            "guided_topics": guided_topics,
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
