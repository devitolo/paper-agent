from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from paper_agents import db
from paper_agents.curator_evidence import assess_evidence
from paper_agents.curator_scoring import SCORING_VERSION, evaluate_candidate

DEFAULT_MAX_RECOMMENDATIONS = 3
DEFAULT_MIN_QUALITY_SCORE = 25.0


@dataclass(frozen=True)
class CuratorConfig:
    max_recommendations: int = DEFAULT_MAX_RECOMMENDATIONS
    min_quality_score: float = DEFAULT_MIN_QUALITY_SCORE
    max_scout_attempts: int = 3
    model: str = SCORING_VERSION
    evidence_enabled: bool = False
    evidence_model: str = "qwen2.5:1.5b-instruct"
    evidence_ollama_url: str = "http://127.0.0.1:11434/api/generate"
    evidence_timeout: int = 45
    evidence_max_chars: int = 7000


class CuratorAgent:
    """Scores a Scout candidate pool and stores evaluations/recommendations."""

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
        max_recommendations = min(config.max_recommendations, DEFAULT_MAX_RECOMMENDATIONS)
        prior_ids = db.recommended_ids_for_cycle(connection, workflow_cycle_id)
        remaining = max(0, max_recommendations - len(prior_ids))
        unique_candidates = {candidate["paper_id"]: candidate for candidate in candidates
                             if candidate["paper_id"] not in prior_ids}
        # Scout writes may still be pending. Never hold that SQLite write lock
        # while the local model processes candidate text.
        connection.commit()
        evaluations = []
        for candidate in unique_candidates.values():
            enriched = {**candidate, "evidence": db.paper_evidence_context(connection, candidate["paper_id"])}
            if config.evidence_enabled:
                enriched["evidence_assessment"] = assess_evidence(
                    enriched, model=config.evidence_model, ollama_url=config.evidence_ollama_url,
                    timeout=config.evidence_timeout, max_chars=config.evidence_max_chars,
                )
            evaluations.append(evaluate_candidate(enriched, profile_version["profile"] if profile_version else {}))
        evaluations.sort(key=lambda item: (item["score"], item.get("published") or ""), reverse=True)

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
            db.insert_recommendation(
                connection,
                curator_run_id=curator_run_id,
                paper_id=recommendation["paper_id"],
                recommendation_order=index,
                rationale=recommendation["rationale"],
            )

        total_recommendations = len(prior_ids) + len(recommendations)
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
                            "evidence_model": config.evidence_model if config.evidence_enabled else None}), curator_run_id),
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
