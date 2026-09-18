"""Development-only replay of human-reviewed relevance and contribution axes."""
from __future__ import annotations

import math
from typing import Any

from paper_agents.curator_scoring import evaluate_candidate

VERSION = "two-axis-development-replay-v1"
QUALITY_DIMENSIONS = (
    "novelty", "technical_depth", "evidence", "baseline_quality", "operational_realism",
)
QUALITY_WEIGHTS = {
    "novelty": 0.20,
    "technical_depth": 0.20,
    "evidence": 0.25,
    "baseline_quality": 0.15,
    "operational_realism": 0.20,
}


def _validate_labels(labels: dict[str, Any], candidate_ids: set[str]) -> dict[str, dict[str, Any]]:
    if labels.get("version") != 1 or labels.get("scale") != {"minimum": 0, "maximum": 4}:
        raise ValueError("unsupported two-axis label contract")
    rows = labels.get("papers")
    if not isinstance(rows, list):
        raise ValueError("labels require papers")
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        paper_id = str(row.get("id") or "") if isinstance(row, dict) else ""
        if not paper_id or paper_id in by_id:
            raise ValueError("label ids must be non-empty and unique")
        for field in ("relevance", *QUALITY_DIMENSIONS):
            value = row.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 4:
                raise ValueError(f"{field} must be an integer from 0 to 4")
        by_id[paper_id] = row
    if set(by_id) != candidate_ids:
        raise ValueError("labels must cover the frozen candidate ids exactly")
    return by_id


def replay_two_axis(corpus: dict[str, Any], labels: dict[str, Any], *, top_k: int = 5) -> dict[str, Any]:
    candidates = corpus.get("candidates")
    profile = corpus.get("profile")
    if not isinstance(candidates, list) or not candidates or not isinstance(profile, dict):
        raise ValueError("corpus requires candidates and profile")
    ids = [str(row.get("id") or "") for row in candidates]
    if any(not value for value in ids) or len(ids) != len(set(ids)):
        raise ValueError("candidate ids must be non-empty and unique")
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= len(candidates):
        raise ValueError("top_k is out of range")
    by_id = _validate_labels(labels, set(ids))
    rows = []
    for candidate in candidates:
        paper_id = str(candidate["id"])
        label = by_id[paper_id]
        baseline = evaluate_candidate(candidate, profile)
        relevance = label["relevance"] * 25.0
        quality = sum(label[field] * 25.0 * QUALITY_WEIGHTS[field] for field in QUALITY_DIMENSIONS)
        # A geometric mean requires strength on both independent axes. It avoids
        # allowing very high topic fit to compensate linearly for zero quality.
        combined = math.sqrt(relevance * quality)
        rows.append({
            "id": paper_id,
            "title": candidate.get("title"),
            "decision": candidate.get("decision"),
            "user_score": candidate.get("user_score"),
            "baseline_score": baseline["score"],
            "review_axes": {
                "relevance": relevance,
                "contribution_quality": round(quality, 3),
                "combined": round(combined, 3),
                "dimension_ratings": {field: label[field] for field in QUALITY_DIMENSIONS},
            },
        })

    def ordered(score_key):
        return sorted(rows, key=lambda row: (-score_key(row), row["id"]))

    baseline_order = ordered(lambda row: row["baseline_score"])
    combined_order = ordered(lambda row: row["review_axes"]["combined"])

    def metrics(order):
        top = order[:top_k]
        return {
            "order": [row["id"] for row in order],
            "top_k": top_k,
            "high_ranked_rejections": [row["id"] for row in top if row["decision"] == "reject"],
            "missed_useful": [row["id"] for row in order[top_k:]
                              if row["decision"] == "keep"
                              and isinstance(row["user_score"], (int, float))
                              and not isinstance(row["user_score"], bool)
                              and row["user_score"] >= 3],
        }

    return {
        "version": VERSION,
        "status": "development_labels_only",
        "automated_assessor_ready": False,
        "disclosure": labels["disclosure"],
        "quality_weights": QUALITY_WEIGHTS,
        "aggregation": "geometric_mean(relevance, contribution_quality)",
        "baseline": metrics(baseline_order),
        "two_axis": metrics(combined_order),
        "results": rows,
        "limitations": [
            "Ratings were derived from previously seen reviews and are not held-out evidence.",
            "This replay tests axis definitions and aggregation, not automated assessment quality.",
            "No production scoring, profile, recommendation, source, or database state is changed.",
        ],
    }
