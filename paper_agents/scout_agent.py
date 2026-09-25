from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
import os
from typing import Any

from paper_agents import db, telemetry
from paper_agents.scout_diagnostics import capture_report
from paper_agents.scout import (
    DEFAULT_ARXIV_REQUEST_DELAY,
    DEFAULT_ARXIV_RETRIES,
    DEFAULT_ARXIV_TIMEOUT,
    DEFAULT_FETCH_LIMIT,
    DEFAULT_FRESHNESS_MONTHS,
    ArxivSource,
    OpenAlexSource,
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
DEFAULT_MIN_ELIGIBLE_CANDIDATES = 10
DEFAULT_MAX_REFILL_FETCH_ROUNDS = 3
SEMANTIC_SCHOLAR_MAX_REFILL_FETCH_ROUNDS = 1


@dataclass(frozen=True)
class ScoutConfig:
    topics: list[str]
    target_candidates: int = DEFAULT_TARGET_CANDIDATES
    min_eligible_candidates: int = DEFAULT_MIN_ELIGIBLE_CANDIDATES
    max_candidates: int = DEFAULT_FETCH_LIMIT
    max_refill_fetch_rounds: int = DEFAULT_MAX_REFILL_FETCH_ROUNDS
    freshness_months: int = DEFAULT_FRESHNESS_MONTHS
    request_delay: float = DEFAULT_ARXIV_REQUEST_DELAY
    retries: int = DEFAULT_ARXIV_RETRIES
    timeout: int = DEFAULT_ARXIV_TIMEOUT
    openalex_cursor_enabled: bool = False


class ScoutAgent:
    """Retrieves source candidates and records a candidate pool without preference scoring."""

    def __init__(self, source: PaperSource | None = None):
        self.source = source

    @telemetry.traced("scout")
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
        telemetry.attributes(workflow_cycle_id=workflow_cycle_id, attempt=attempt_number, source=source.name)
        active_guidance = db.active_scouting_guidance(connection)
        scout_guidance = load_scout_guidance(connection)
        guided_topics = topics_with_guidance(config.topics, scout_guidance)
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

        # Source requests may spend minutes retrying rate limits. Persist the run
        telemetry.attributes(scout_run_id=scout_run_id, guidance_id=guidance_id)
        # setup first so another scheduled pipeline is not blocked meanwhile.
        connection.commit()

        warnings: list[str] = []
        errors: list[str] = []
        progress = None
        if isinstance(source, OpenAlexSource) and (
                config.openalex_cursor_enabled or os.getenv("PAPER_OPENALEX_CURSOR") == "1"):
            from paper_agents.openalex_progress import OpenAlexProgress
            progress = OpenAlexProgress(connection, source)
            candidates = progress.fetch(guided_topics, freshness_months=config.freshness_months,
                                        max_candidates=config.max_candidates, errors=errors)
            guided_candidates = apply_guidance_to_candidates(candidates, scout_guidance)
            source_diagnostics = progress.diagnostics
            refill_diagnostics = {"stop_reason": source_diagnostics["stop_reason"],
                                  "rounds": [], "mode": "cursor_v1"}
        else:
            candidates, guided_candidates, refill_diagnostics, source_diagnostics = self._fetch_candidate_pool(
                connection,
                source=source,
                source_topics=config.topics if source.name == "semantic_scholar" else guided_topics,
                guidance=scout_guidance,
                config=config,
                errors=errors,
            )
        # Cursor advancement, whole-page ledger and candidate dispositions are atomic.
        with connection if progress else nullcontext():
            if progress:
                connection.execute("BEGIN IMMEDIATE")
                progress.checkpoint(scout_run_id)
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
                    "source_topics": config.topics
                    if source.name == "semantic_scholar"
                    else guided_topics,
                    "source_diagnostics": source_diagnostics,
                    "refill": refill_diagnostics,
                },
                warnings=warnings,
                errors=errors,
            )
            db.update_workflow_state(connection, workflow_cycle_id, "scout_complete")
            capture_report(connection, scout_run_id, phase="scout_complete")
            telemetry.attributes(error_count=len(errors), warning_count=len(warnings))
            if errors:
                telemetry.failure()
            return {
                "scout_run_id": scout_run_id,
                "source": source.name,
                "attempt_number": attempt_number,
                "guidance": guidance_summary(scout_guidance),
                "base_topics": config.topics,
                "guided_topics": guided_topics,
                "source_topics": config.topics
                if source.name == "semantic_scholar"
                else guided_topics,
                "fetched_count": len(candidates),
                "stored_count": len(stored),
                "eligible_count": len([candidate for candidate in stored if not candidate["excluded"]]),
                "warnings": warnings,
                "errors": errors,
                "source_degraded": source_diagnostics.get("coverage_mode") == "single_result_406_fallback",
                "candidates": stored,
            }

    def _fetch_candidate_pool(
        self,
        connection,
        *,
        source: PaperSource,
        source_topics: list[str],
        guidance,
        config: ScoutConfig,
        errors: list[str],
    ) -> tuple[list[Any], list[tuple[Any, dict[str, Any]]], dict[str, Any], dict[str, Any]]:
        """Refill past previously discovered search results before Curator sees the pool."""
        known_keys = {
            row[0]
            for row in connection.execute("SELECT canonical_key FROM papers").fetchall()
            if row[0]
        }
        candidates: list[Any] = []
        guided_candidates: list[tuple[Any, dict[str, Any]]] = []
        source_diagnostics: dict[str, Any] = {}
        rounds: list[dict[str, Any]] = []
        requested_limit = max(1, config.max_candidates)
        # Small/manual runs retain their requested pool size; scheduled source
        # jobs fetch 20-30 initially and therefore refill toward ten candidates.
        target = min(max(1, config.min_eligible_candidates), max(1, config.max_candidates))
        max_rounds = max(1, config.max_refill_fetch_rounds)
        if source.name == "semantic_scholar":
            max_rounds = min(max_rounds, SEMANTIC_SCHOLAR_MAX_REFILL_FETCH_ROUNDS)
        stop_reason = "max_fetch_rounds_reached"

        for round_number in range(1, max_rounds + 1):
            previous_count = len(candidates)
            try:
                fetched = source.fetch(
                    source_topics,
                    max_results=requested_limit,
                    freshness_months=config.freshness_months,
                )
            except Exception as error:  # Source adapters normalize most errors, but keep runs recoverable.
                errors.append(str(error))
                source_diagnostics = dict(getattr(source, "last_diagnostics", {}) or {})
                stop_reason = "source_error"
                break

            candidates = dedupe_candidates([*candidates, *fetched])
            guided_candidates = apply_guidance_to_candidates(candidates, guidance)
            estimated_eligible = sum(
                1
                for candidate, diagnostics in guided_candidates
                if db.canonical_key_for_candidate(sanitize_candidate(candidate.as_dict())) not in known_keys
                and not diagnostics.get("feedback_guidance_excluded")
            )
            source_diagnostics = dict(getattr(source, "last_diagnostics", {}) or {})
            rounds.append(
                {
                    "round": round_number,
                    "fetch_limit": requested_limit,
                    "returned_count": len(fetched),
                    "unique_count": len(candidates),
                    "estimated_eligible_count": estimated_eligible,
                }
            )

            if source_diagnostics.get("coverage_mode") == "single_result_406_fallback":
                stop_reason = "source_degraded"
                break
            if source.name in {"arxiv", "semantic_scholar"} and getattr(source, "cooldown_active", False):
                stop_reason = "source_cooldown"
                break
            if estimated_eligible >= target:
                stop_reason = "minimum_eligible_reached"
                break
            if round_number > 1 and len(candidates) == previous_count:
                stop_reason = "source_exhausted"
                break
            requested_limit += max(1, config.max_candidates)

        return candidates, guided_candidates, {
            "minimum_eligible_candidates": target,
            "rounds": rounds,
            "stop_reason": stop_reason,
        }, source_diagnostics


def sanitize_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    clean = dict(candidate)
    clean.pop("score", None)
    clean.pop("matched_keywords", None)
    clean.pop("ranking_reason", None)
    clean.pop("selected", None)
    return clean
