"""Deterministic Curator V2 policy; scores express fit, not probability."""
from __future__ import annotations

import math
import re
from typing import Any

from paper_agents.scout import OFF_DOMAIN_TERMS, count_phrase, normalize_text, scout_keywords

SCORING_VERSION = "deterministic-v2"
# Remove grammatical and evaluative filler, not domain-specific vocabulary.
STOP_WORDS = set("a an the and or for to of in on with without by from as at is are be that this those these it its their our my me i want more less papers paper work works research studies study approach approaches use uses using based practical connection surrounding particular overly only relevant relevance focus focused interested interests prefer preference signals signal".split())
ADVERSE_WORDS = set("weak unrealistic theoretical synthetic toy superficial illustrative repackaging overhead ignoring lacks removing isolated isolation".split())


def words(text: str) -> set[str]:
    return {word for word in re.findall(r"[a-z0-9]+", normalize_text(text))
            if len(word) > 2 and word not in STOP_WORDS}


def profile_matches(signals: list[str], text: str, *, negative: bool = False) -> list[dict[str, Any]]:
    text_words = words(text)
    matches = []
    seen = set()
    for signal in signals:
        phrase = normalize_text(str(signal))
        terms = words(phrase)
        if not phrase or not terms or phrase in seen:
            continue
        seen.add(phrase)
        hits = terms & text_words
        exact = bool(count_phrase(text, phrase))
        # Prose preferences need multiple substantive components, not a lone
        # generic word. Negative prose also needs its adverse qualifier present.
        component_match = len(hits) >= 2 and len(hits) / len(terms) >= 0.4
        qualifiers = terms & ADVERSE_WORDS
        if negative and qualifiers and not qualifiers & hits:
            component_match = False
        if exact or component_match:
            matches.append({"signal": str(signal), "terms": sorted(hits),
                            "match": "phrase" if exact else "components"})
    return matches


def evidence_context(candidate: dict[str, Any]) -> dict[str, Any]:
    provenance = candidate.get("evidence") or {}
    abstract_only = bool(provenance.get("abstract_only") or candidate.get("abstract_only"))
    full_triage = bool(provenance.get("full_text_triage")) and not abstract_only
    pdf = bool(provenance.get("pdf_artifact"))
    has_abstract = bool(str(candidate.get("abstract") or "").strip())
    if full_triage:
        level, confidence, bonus, ceiling = "full_text_triage", 1.0, 15.0, 100.0
    elif abstract_only:
        level, confidence, bonus, ceiling = "abstract_only_triage", 0.9, 0.0, 85.0
    elif pdf:
        level, confidence, bonus, ceiling = "pdf_available_unreviewed", 0.95, 5.0, 90.0
    elif has_abstract:
        level, confidence, bonus, ceiling = "source_abstract", 0.9, 0.0, 85.0
    else:
        level, confidence, bonus, ceiling = "metadata_only", 0.8, 0.0, 70.0
    return {"level": level, "confidence": confidence, "bonus": bonus, "ceiling": ceiling,
            "provenance": provenance}


def evaluate_candidate(candidate: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    title = normalize_text(candidate.get("title") or "")
    text = normalize_text(" ".join([candidate.get("title") or "", candidate.get("abstract") or ""]))
    keywords = scout_keywords([])
    matches = [term for term in keywords if count_phrase(text, term)]
    # Each distinct phrase contributes once per field. Repetition cannot pump
    # the score; a smooth topic curve leaves room for profile and evidence.
    raw_topic = sum(keywords[term] * (4 if count_phrase(title, term) else 1) for term in matches)
    topic_score = 60.0 * -math.expm1(-raw_topic / 45.0)
    positive = profile_matches(list(profile.get("interests") or []) + list(profile.get("positive_signals") or []), text)
    negative = profile_matches(list(profile.get("negative_signals") or []), text, negative=True)
    positive_terms = {term for match in positive for term in match["terms"]}
    profile_score = 25.0 * -math.expm1(-len(positive_terms) / 4.0)
    # Overlapping/repeated prose with identical matched terms is one penalty.
    negative_patterns = {tuple(match["terms"]) for match in negative}
    negative_penalty = min(36.0, 12.0 * len(negative_patterns))
    off_domain = [term for term in OFF_DOMAIN_TERMS if count_phrase(text, term)]
    off_domain_penalty = min(60.0, 12.0 * len(off_domain))
    evidence = evidence_context(candidate)
    relevance = topic_score + profile_score
    # Artifact presence cannot make an unrelated paper relevant.
    evidence_bonus = evidence["bonus"] * min(1.0, relevance / 50.0)
    before_penalties = min(evidence["ceiling"], relevance * evidence["confidence"] + evidence_bonus)
    score = round(max(0.0, before_penalties - negative_penalty - off_domain_penalty), 2)
    components = {
        "version": SCORING_VERSION, "raw_topic": round(raw_topic, 3),
        "topic": round(topic_score, 3), "profile": round(profile_score, 3),
        "positive_matches": positive, "negative_matches": negative,
        "negative_penalty": negative_penalty, "off_domain_penalty": off_domain_penalty,
        "off_domain_matches": off_domain, "evidence": evidence,
        "evidence_bonus": round(evidence_bonus, 3), "before_penalties": round(before_penalties, 3),
    }
    rationale = (f"Curator V2: topic {topic_score:.1f}, profile {profile_score:.1f}; "
                 f"evidence {evidence['level']} (confidence {evidence['confidence']:.2f}); "
                 f"evidence bonus {evidence_bonus:.1f}.")
    if matches:
        rationale += " Matched " + ", ".join(matches[:8]) + "."
    if positive:
        rationale += " Profile components: " + ", ".join(sorted(positive_terms)[:12]) + "."
    if negative:
        rationale += f" Penalized for negative signals (-{negative_penalty:.1f}): " + "; ".join(m["signal"] for m in negative[:4]) + "."
    if off_domain:
        rationale += f" Penalized for off-domain signals (-{off_domain_penalty:.1f}): " + ", ".join(off_domain[:4]) + "."
    return {**candidate, "score": score, "matched_signals": matches,
            "rationale": rationale, "score_components": components}
