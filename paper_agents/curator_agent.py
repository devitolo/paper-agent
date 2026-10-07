from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from paper_agents import db, telemetry
from paper_agents.curator_evidence import DEFAULT_CURATOR_MODEL, assess_evidence
from paper_agents.curator_interest import (
    default_interest_scorer,
    positive_interest_descriptions,
    score_interest_fit,
    unavailable_interest_fit,
)
from paper_agents.curator_scoring import SCORING_VERSION, evaluate_candidate
from paper_agents.paper_eligibility import curator_eligibility
from paper_agents.runtime_config import ollama_url
from paper_agents.local_extract import unload_ollama_model

DEFAULT_MAX_RECOMMENDATIONS = 3
DEFAULT_MIN_QUALITY_SCORE = 25.0


@dataclass(frozen=True)
class CuratorConfig:
    max_recommendations: int = DEFAULT_MAX_RECOMMENDATIONS
    min_quality_score: float = DEFAULT_MIN_QUALITY_SCORE
    max_scout_attempts: int = 3
    model: str = SCORING_VERSION
    evidence_enabled: bool = False
    evidence_model: str = DEFAULT_CURATOR_MODEL
    evidence_ollama_url: str = field(default_factory=ollama_url)
    evidence_timeout: int = 45
    evidence_max_chars: int = 7000
    evidence_candidate_limit: int = 10
    interest_fit_enabled: bool = True
    interest_descriptions: tuple[str, ...] = ()
    interest_fit_scorer: Callable[[str, str, str], float] | None = field(
        default=None, repr=False, compare=False
    )


class CuratorAgent:
    """Scores a Scout candidate pool and stores evaluations/recommendations."""

    @telemetry.traced("curator", scoring_version=SCORING_VERSION)
    def run(
        self,
        connection,
        *,
        workflow_cycle_id: int,
        candidates: list[dict[str, Any]],
        profile_version: dict[str, Any] | None,
        scout_attempt_count: int,
        config: CuratorConfig,
    ) -> dict[str, Any]:
        telemetry.attributes(workflow_cycle_id=workflow_cycle_id, attempt=scout_attempt_count,
                             profile_version_id=profile_version["id"] if profile_version else None)
        max_recommendations = min(config.max_recommendations, DEFAULT_MAX_RECOMMENDATIONS)
        prior_ids = db.recommended_ids_for_cycle(connection, workflow_cycle_id)
        remaining = max(0, max_recommendations - len(prior_ids))
        unique_candidates = {candidate["paper_id"]: candidate for candidate in candidates
                             if candidate["paper_id"] not in prior_ids}
        # Scout writes may still be pending. Never hold that SQLite write lock
        # while the local model processes candidate text.
        connection.commit()
        profile = profile_version["profile"] if profile_version else {}
        interests = positive_interest_descriptions(profile, config.interest_descriptions)
        interest_scorer = config.interest_fit_scorer
        interest_runtime_error: str | None = None
        if config.interest_fit_enabled and interests and interest_scorer is None:
            try:
                interest_scorer = default_interest_scorer()
            except Exception as error:
                interest_runtime_error = str(error)

        def apply_eligibility(evaluation: dict[str, Any], enriched: dict[str, Any]) -> None:
            eligibility = curator_eligibility(enriched)
            evaluation["curator_eligibility"] = eligibility
            evaluation["score_components"]["curator_eligibility"] = eligibility
            if eligibility["disqualified"]:
                evaluation["score"] = 0.0
                evaluation["rationale"] += f" Curator eligibility blocked recommendation: {eligibility['disqualification_reason']}."
            elif eligibility["labels"]:
                evaluation["rationale"] += " Curator labels: " + ", ".join(eligibility["labels"]) + "."

        evaluations = []
        for candidate in unique_candidates.values():
            with telemetry.span("curator.evaluate", paper_id=candidate["paper_id"],
                                scout_candidate_id=candidate.get("scout_candidate_id"), scoring_version=SCORING_VERSION):
                enriched = {**candidate, "evidence": db.paper_evidence_context(connection, candidate["paper_id"])}
                if not config.interest_fit_enabled:
                    interest_fit = unavailable_interest_fit("disabled")
                elif interest_runtime_error is not None:
                    interest_fit = unavailable_interest_fit("unavailable", error=interest_runtime_error)
                elif interest_scorer is None:
                    interest_fit = unavailable_interest_fit("no_positive_interests")
                else:
                    interest_fit = score_interest_fit(enriched, interests, interest_scorer)
                evaluation = evaluate_candidate(enriched, profile)
                apply_eligibility(evaluation, enriched)
                evaluation["interest_fit"] = interest_fit
                evaluation["score_components"]["interest_fit"] = interest_fit
                evaluations.append(evaluation)

        evaluations.sort(
            key=lambda item: (
                item["interest_fit"]["status"] == "scored",
                item["interest_fit"].get("score")
                if item["interest_fit"]["status"] == "scored" else float("-inf"),
                item["score"],
                item.get("published") or "",
            ),
            reverse=True,
        )
        try:
            if config.evidence_enabled:
                for index, preliminary in enumerate(evaluations[:max(1, config.evidence_candidate_limit)]):
                    enriched = {**preliminary, "evidence_assessment": assess_evidence(
                        preliminary, model=config.evidence_model, ollama_url=config.evidence_ollama_url,
                        timeout=config.evidence_timeout, max_chars=config.evidence_max_chars,
                    )}
                    evaluation = evaluate_candidate(enriched, profile)
                    apply_eligibility(evaluation, enriched)
                    evaluation["interest_fit"] = preliminary["interest_fit"]
                    evaluation["score_components"]["interest_fit"] = preliminary["interest_fit"]
                    evaluations[index] = evaluation
        finally:
            if config.evidence_enabled:
                try:
                    unload_ollama_model(config.evidence_ollama_url, config.evidence_model)
                except (OSError, TimeoutError, ValueError):
                    telemetry.event("curator_model_unload_failed", model=config.evidence_model)
        evaluations.sort(
            key=lambda item: (
                item["interest_fit"]["status"] == "scored",
                item["interest_fit"].get("score")
                if item["interest_fit"]["status"] == "scored" else float("-inf"),
                item["score"],
                item.get("published") or "",
            ),
            reverse=True,
        )
        scored_rank = 0
        for evaluation in evaluations:
            if evaluation["interest_fit"]["status"] == "scored":
                scored_rank += 1
                evaluation["interest_fit"]["rank"] = scored_rank
            else:
                evaluation["interest_fit"]["rank"] = None

        db.update_workflow_state(connection, workflow_cycle_id, "curating")
        curator_run_id = db.create_curator_run(
            connection,
            workflow_cycle_id=workflow_cycle_id,
            profile_version_id=profile_version["id"] if profile_version else None,
            scout_attempt_count=scout_attempt_count,
            max_scout_attempts=config.max_scout_attempts,
            min_quality_score=config.min_quality_score,
            max_recommendations=max_recommendations,
            model=config.model,
        )

        for evaluation in evaluations:
            db.insert_curator_evaluation(
                connection,
                curator_run_id=curator_run_id,
                paper_id=evaluation["paper_id"],
                scout_candidate_id=evaluation.get("scout_candidate_id"),
                score=evaluation["score"],
                rationale=evaluation["rationale"],
                matched_signals=evaluation["matched_signals"],
                quality_threshold_met=evaluation["score"] >= config.min_quality_score,
            )

        recommendations = [
            evaluation for evaluation in evaluations if evaluation["score"] >= config.min_quality_score
        ][:remaining]
        for index, recommendation in enumerate(recommendations, 1):
            recommendation_id = db.insert_recommendation(
                connection,
                curator_run_id=curator_run_id,
                paper_id=recommendation["paper_id"],
                recommendation_order=index,
                rationale=recommendation["rationale"],
            )
            telemetry.event("recommendation", recommendation_id=recommendation_id,
                            paper_id=recommendation["paper_id"], curator_run_id=curator_run_id)

        total_recommendations = len(prior_ids) + len(recommendations)
        telemetry.attributes(candidate_count=len(evaluations), recommendation_count=len(recommendations),
                             curator_run_id=curator_run_id)
        requested_rescout = total_recommendations < max_recommendations and scout_attempt_count < config.max_scout_attempts
        rescout_reason = None
        if requested_rescout:
            rescout_reason = (
                f"Only {total_recommendations} distinct candidates met the quality threshold "
                f"of {config.min_quality_score}."
            )
        db.update_curator_rescout(connection, curator_run_id, requested=requested_rescout, reason=rescout_reason)
        connection.execute(
            "UPDATE curator_runs SET metadata_json = ? WHERE id = ?",
            (db.json_dumps({"scoring_version": SCORING_VERSION,
                            "evaluations": {str(e["paper_id"]): e["score_components"] for e in evaluations},
                            "prior_cycle_recommendations": len(prior_ids),
                            "evidence_enabled": config.evidence_enabled,
                            "evidence_model": config.evidence_model if config.evidence_enabled else None,
                            "evidence_candidate_limit": config.evidence_candidate_limit,
                            "interest_fit_enabled": config.interest_fit_enabled,
                            "interest_fit_interests": interests,
                            "interest_fit_runtime_error": interest_runtime_error}), curator_run_id),
        )

        guidance_text = build_guidance(evaluations, recommendations)
        guidance_id = db.create_scouting_guidance(
            connection,
            curator_run_id=curator_run_id,
            guidance_text=guidance_text,
            metadata={"source": "curator", "scout_attempt_count": scout_attempt_count},
            active=True,
        )

        db.update_workflow_state(
            connection,
            workflow_cycle_id,
            "rescout_requested" if requested_rescout else "recommendations_ready",
        )
        return {
            "curator_run_id": curator_run_id,
            "guidance_id": guidance_id,
            "evaluations": evaluations,
            "recommendations": recommendations,
            "requested_rescout": requested_rescout,
            "rescout_reason": rescout_reason,
        }


def build_guidance(evaluations: list[dict[str, Any]], recommendations: list[dict[str, Any]]) -> str:
    if recommendations:
        signals: list[str] = []
        for recommendation in recommendations:
            for signal in recommendation.get("matched_signals") or []:
                if signal not in signals:
                    signals.append(signal)
        if signals:
            return "Prioritize papers with these signals: " + ", ".join(signals[:8]) + "."
    if evaluations:
        return "Broaden search terms while keeping focus on AI for production operations, incident response, and engineering workflows."
    return "Broaden search terms; the previous Scout run returned no usable candidates."
