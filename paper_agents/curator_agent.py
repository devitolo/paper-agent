from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from paper_agents import db
from paper_agents.scout import count_phrase, normalize_text, scout_keywords
from paper_agents.scout import OFF_DOMAIN_TERMS

DEFAULT_MAX_RECOMMENDATIONS = 3
DEFAULT_MIN_QUALITY_SCORE = 25.0


@dataclass(frozen=True)
class CuratorConfig:
    max_recommendations: int = DEFAULT_MAX_RECOMMENDATIONS
    min_quality_score: float = DEFAULT_MIN_QUALITY_SCORE
    max_scout_attempts: int = 3
    model: str = "deterministic-v1"


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
        db.update_workflow_state(connection, workflow_cycle_id, "curating")
        max_recommendations = min(config.max_recommendations, DEFAULT_MAX_RECOMMENDATIONS)
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

        evaluations = [evaluate_candidate(candidate, profile_version["profile"] if profile_version else {}) for candidate in candidates]
        evaluations.sort(key=lambda item: (item["score"], item.get("published") or ""), reverse=True)

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
        ][:max_recommendations]
        for index, recommendation in enumerate(recommendations, 1):
            db.insert_recommendation(
                connection,
                curator_run_id=curator_run_id,
                paper_id=recommendation["paper_id"],
                recommendation_order=index,
                rationale=recommendation["rationale"],
            )

        requested_rescout = len(recommendations) < max_recommendations and scout_attempt_count < config.max_scout_attempts
        rescout_reason = None
        if requested_rescout:
            rescout_reason = (
                f"Only {len(recommendations)} candidates met the quality threshold "
                f"of {config.min_quality_score}."
            )
        db.update_curator_rescout(connection, curator_run_id, requested=requested_rescout, reason=rescout_reason)

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


def evaluate_candidate(candidate: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    topics = list(profile.get("interests") or [])
    topics.extend(profile.get("positive_signals") or [])
    keywords = scout_keywords(topics)
    text = normalize_text(" ".join([candidate.get("title") or "", candidate.get("abstract") or ""]))
    score = 0.0
    matches: list[str] = []
    for keyword, weight in keywords.items():
        hits = count_phrase(text, keyword)
        if hits:
            title_hits = count_phrase(normalize_text(candidate.get("title") or ""), keyword)
            score += min(hits, 4) * weight
            score += title_hits * weight * 3
            matches.append(keyword)

    negative_hits = []
    for signal in profile.get("negative_signals") or []:
        normalized = normalize_text(signal)
        if normalized and count_phrase(text, normalized):
            negative_hits.append(signal)
            score -= 10.0

    off_domain_hits = [term for term in OFF_DOMAIN_TERMS if count_phrase(text, term)]
    if off_domain_hits:
        score -= min(len(off_domain_hits), 5) * 12.0

    score = round(min(max(score, 0.0), 100.0), 2)
    if matches:
        rationale = "Matched " + ", ".join(matches[:8]) + "."
    else:
        rationale = "No strong profile signals matched."
    if negative_hits:
        rationale += " Penalized for negative signals: " + ", ".join(negative_hits[:4]) + "."
    if off_domain_hits:
        rationale += " Penalized for off-domain signals: " + ", ".join(off_domain_hits[:4]) + "."
    return {
        **candidate,
        "score": score,
        "matched_signals": matches,
        "rationale": rationale,
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
