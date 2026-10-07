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
    CoreSource,
    OpenAlexSource,
    PaperSource,
    SemanticScholarSource,
    dedupe_candidates,
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
    arxiv_progress_enabled: bool = False
    openalex_cursor_enabled: bool = False
    semantic_scholar_progress_enabled: bool = False
    core_progress_enabled: bool = False


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
            guidance_id=None,
            diagnostics={"configured_topics": config.topics},
        )

        # Source requests may spend minutes retrying rate limits. Persist the run
        telemetry.attributes(scout_run_id=scout_run_id)
        # setup first so another scheduled pipeline is not blocked meanwhile.
        connection.commit()

        warnings: list[str] = []
        errors: list[str] = []
        progress = None
        if isinstance(source, ArxivSource) and (
                config.arxiv_progress_enabled
                or os.getenv("PAPER_ARXIV_PROGRESS") == "1"):
            from paper_agents.arxiv_progress import ArxivProgress
            progress = ArxivProgress(connection, source)
            candidates = progress.fetch(
                config.topics,
                freshness_months=config.freshness_months,
                max_candidates=config.max_candidates,
                errors=errors,
            )
            ordered_candidates = [(candidate, {}) for candidate in candidates]
            source_diagnostics = progress.diagnostics
            refill_diagnostics = {
                "stop_reason": source_diagnostics["stop_reason"],
                "rounds": [],
                "mode": "offset_v1",
            }
        elif isinstance(source, SemanticScholarSource) and (
                config.semantic_scholar_progress_enabled
                or os.getenv("PAPER_SEMANTIC_SCHOLAR_PROGRESS") == "1"):
            from paper_agents.semantic_scholar_progress import SemanticScholarProgress
            progress = SemanticScholarProgress(connection, source)
            candidates = progress.fetch(
                config.topics,
                freshness_months=config.freshness_months,
                max_candidates=config.max_candidates,
                errors=errors,
            )
            ordered_candidates = [(candidate, {}) for candidate in candidates]
            source_diagnostics = progress.diagnostics
            refill_diagnostics = {
                "stop_reason": source_diagnostics["stop_reason"],
                "rounds": [],
                "mode": "offset_v1",
            }
        elif isinstance(source, OpenAlexSource) and (
                config.openalex_cursor_enabled or os.getenv("PAPER_OPENALEX_CURSOR") == "1"):
            from paper_agents.openalex_progress import OpenAlexProgress
            progress = OpenAlexProgress(connection, source)
            candidates = progress.fetch(config.topics, freshness_months=config.freshness_months,
                                        max_candidates=config.max_candidates, errors=errors)
            ordered_candidates = [(candidate, {}) for candidate in candidates]
            source_diagnostics = progress.diagnostics
            refill_diagnostics = {"stop_reason": source_diagnostics["stop_reason"],
                                  "rounds": [], "mode": "cursor_v1"}
        elif isinstance(source, CoreSource) and (
                config.core_progress_enabled or os.getenv("PAPER_CORE_SOURCE") == "1"):
            from paper_agents.core_progress import CoreProgress
            progress = CoreProgress(connection, source)
            candidates = progress.fetch(config.topics, freshness_months=config.freshness_months,
                                        max_candidates=config.max_candidates, errors=errors)
            ordered_candidates = [(candidate, {}) for candidate in candidates]
            source_diagnostics = progress.diagnostics
            refill_diagnostics = {"stop_reason": source_diagnostics["stop_reason"],
                                  "rounds": [], "mode": "offset_v1"}
        else:
            candidates, ordered_candidates, refill_diagnostics, source_diagnostics = self._fetch_candidate_pool(
                connection,
                source=source,
                source_topics=config.topics,
                config=config,
                errors=errors,
            )
        # Cursor advancement, whole-page ledger and candidate dispositions are atomic.
        with connection if progress else nullcontext():
            if progress:
                connection.execute("BEGIN IMMEDIATE")
                progress.checkpoint(scout_run_id)
            stored: list[dict[str, Any]] = []
            for index, (candidate, source_annotation) in enumerate(ordered_candidates, 1):
                candidate_dict = sanitize_candidate(candidate.as_dict())
                paper_id, is_new = db.upsert_paper(connection, candidate_dict)
                seen_in_current_cycle = db.paper_has_scout_candidate_in_cycle(
                    connection,
                    paper_id=paper_id,
                    workflow_cycle_id=workflow_cycle_id,
                    before_scout_run_id=scout_run_id,
                )
                already_recommended = db.paper_has_prior_recommendation(
                    connection,
                    paper_id=paper_id,
                    before_scout_run_id=scout_run_id,
                )
                excluded = already_recommended or (not is_new and not seen_in_current_cycle)
                exclusion_reason = None
                if already_recommended:
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
                        **source_annotation,
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
                    "configured_topics": config.topics,
                    "source_topics": config.topics,
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
                "configured_topics": config.topics,
                "source_topics": config.topics,
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
        ordered_candidates: list[tuple[Any, dict[str, Any]]] = []
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
            ordered_candidates = [(candidate, {}) for candidate in candidates]
            estimated_eligible = sum(
                1
                for candidate, _ in ordered_candidates
                if db.canonical_key_for_candidate(sanitize_candidate(candidate.as_dict())) not in known_keys
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

        return candidates, ordered_candidates, {
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
