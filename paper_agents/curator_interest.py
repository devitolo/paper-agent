"""Curator-side topical interest fit using the pinned local MiniLM model."""
from __future__ import annotations

import math
import re
from typing import Any, Callable

from paper_agents.minilm_eval import MODEL_ID, MODEL_REVISION, MODEL_SHA256, QUERY_VERSION, MiniLMScorer

MIN_ABSTRACT_CHARS = 80
INVALID_ABSTRACTS = {"none", "null", "n/a", "na", "no abstract", "abstract unavailable"}


def positive_interest_descriptions(profile: dict[str, Any], configured: tuple[str, ...] = ()) -> list[str]:
    """Return positive topical descriptions only; preference penalties stay out."""
    values = [*(profile.get("interests") or []), *configured]
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = " ".join(str(value or "").split())
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def abstract_status(value: Any) -> tuple[str, str | None]:
    abstract = " ".join(str(value or "").split())
    if not abstract:
        return "missing_abstract", None
    if abstract.casefold() in INVALID_ABSTRACTS or len(abstract) < MIN_ABSTRACT_CHARS:
        return "invalid_abstract", abstract
    if not re.search(r"[a-zA-Z]", abstract):
        return "invalid_abstract", abstract
    return "valid", abstract


def interest_fit_provenance() -> dict[str, Any]:
    return {
        "model_id": MODEL_ID,
        "model_revision": MODEL_REVISION,
        "model_sha256": MODEL_SHA256,
        "query_version": QUERY_VERSION,
        "aggregation": "max",
        "input": "title_plus_abstract",
        "preference_scope": "positive_topical_interests_only",
    }


def unavailable_interest_fit(status: str, *, error: str | None = None) -> dict[str, Any]:
    result = {"status": status, "score": None, "matched_interest": None,
              "per_interest": [], **interest_fit_provenance()}
    if error:
        result["error"] = error[:240]
    return result


def score_interest_fit(
    candidate: dict[str, Any],
    interests: list[str],
    scorer: Callable[[str, str, str], float],
) -> dict[str, Any]:
    if not interests:
        return unavailable_interest_fit("no_positive_interests")
    status, abstract = abstract_status(candidate.get("abstract"))
    if status != "valid":
        return unavailable_interest_fit(status)
    title = " ".join(str(candidate.get("title") or "").split())
    if not title:
        return unavailable_interest_fit("missing_title")
    try:
        per_interest = []
        for interest in interests:
            score = float(scorer(title, abstract or "", interest))
            if not math.isfinite(score):
                raise RuntimeError("MiniLM returned a non-finite score")
            per_interest.append({"interest": interest, "score": score})
    except Exception as error:
        return unavailable_interest_fit("unavailable", error=str(error))
    best = max(per_interest, key=lambda item: item["score"])
    return {
        "status": "scored",
        "score": best["score"],
        "matched_interest": best["interest"],
        "per_interest": per_interest,
        **interest_fit_provenance(),
    }


def default_interest_scorer() -> Callable[[str, str, str], float]:
    model = MiniLMScorer()
    return model.score_pair
