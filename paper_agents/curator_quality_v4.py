"""Offline-only contribution-aware ranking proposal.

This module is deliberately not imported by the production Curator.  It exists
to make a proposed ranking policy replayable before any production activation.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from paper_agents.curator_scoring import evaluate_candidate


SCORING_VERSION = "contribution-aware-v4-proposal"
PASSAGE_VERSION = "targeted-passages-v1"
SECTION_KINDS = ("method", "evaluation", "limitations", "conclusion")
CONTRIBUTION_TYPES = {
    "empirical", "systems", "architecture", "perspective", "framework",
    "survey", "theoretical", "unknown",
}
CLAIM_KINDS = (
    "measurements", "baselines", "reasoning", "tradeoffs",
    "actionable_insight", "limitations",
)
CLAIM_STATES = {"present", "absent", "unknown"}

_SECTION_TERMS = {
    "method": ("method", "methods", "approach", "approaches", "design", "designs",
               "architecture", "architectures", "implementation", "implementations",
               "system", "systems"),
    "evaluation": ("evaluation", "evaluations", "experiment", "experiments", "result",
                   "results", "benchmark", "benchmarks", "measurement", "measurements",
                   "study", "studies"),
    "limitations": ("limitation", "limitations", "threat", "threats", "caveat", "caveats",
                    "constraint", "constraints", "future work"),
    "conclusion": ("conclusion", "conclusions", "discussion", "discussions", "implication",
                   "implications", "recommendation", "recommendations", "lesson", "lessons"),
}


def _paragraphs(text: str) -> list[tuple[int, int, str]]:
    return [(match.start(), match.end(), match.group(0).strip())
            for match in re.finditer(r"\S(?:.*?\S)?(?=\n\s*\n|\s*\Z)", text, re.DOTALL)
            if match.group(0).strip()]


def _heading_kind(paragraph: str) -> str | None:
    raw_first = paragraph.splitlines()[0].strip().strip("0123456789.:- ")
    # Lowercase prose such as "recommendation is ..." is evidence text, not a
    # section heading. It remains eligible for keyword fallback.
    if raw_first and raw_first[0].islower():
        return None
    first = raw_first.lower()
    first = re.sub(r"^(?:[ivxlcdm]+|[a-z])\s*[.):-]\s*", "", first).strip()
    if len(first) > 100:
        return None
    for kind, terms in _SECTION_TERMS.items():
        if any(first == term or first.startswith(term + " ") for term in terms):
            return kind
    return None


def _keyword_fallback_matches(kind: str, paragraph: str) -> bool:
    """Find an unconventional section lead without matching incidental body mentions."""
    lead = paragraph[:180].lower()
    return any(re.search(rf"\b{re.escape(term)}\b", lead) for term in _SECTION_TERMS[kind])


def select_targeted_passages(
    text: str,
    *,
    max_total_chars: int = 7000,
    max_section_chars: int = 2000,
    max_passages: int = 8,
) -> dict[str, Any]:
    """Select bounded exact passages and report the scope actually inspected."""
    if max_total_chars < 1 or max_section_chars < 1 or max_passages < 1:
        raise ValueError("passage budgets must be positive")
    if not text.strip():
        return {
            "version": PASSAGE_VERSION,
            "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "source_chars": len(text),
            "selected_chars": 0,
            "truncated": False,
            "passages": [],
            "sections": {kind: {"state": "unavailable", "selected_chars": 0,
                                "heading_found": False, "selected_passages_complete": False}
                         for kind in SECTION_KINDS},
        }

    paragraphs = _paragraphs(text)
    headings: dict[str, list[int]] = {kind: [] for kind in SECTION_KINDS}
    keyword_candidates: dict[str, list[tuple[int, int, str]]] = {kind: [] for kind in SECTION_KINDS}
    for index, (_, _, paragraph) in enumerate(paragraphs):
        kind = _heading_kind(paragraph)
        if kind:
            headings[kind].append(index)
        for section_kind in _SECTION_TERMS:
            if _keyword_fallback_matches(section_kind, paragraph):
                keyword_candidates[section_kind].append(paragraphs[index])

    selected: list[dict[str, Any]] = []
    used_ranges: set[tuple[int, int]] = set()
    section_chars = {kind: 0 for kind in SECTION_KINDS}
    total = 0
    for kind in SECTION_KINDS:
        candidates: list[tuple[int, int, str, str]] = []
        for heading_index in headings[kind]:
            # Include the heading paragraph and bounded following paragraphs up
            # to the next recognized heading. This is an inspected excerpt,
            # never a claim that the entire paper or section was inspected.
            heading_has_body = len(paragraphs[heading_index][2].splitlines()) > 1
            first_index = heading_index if heading_has_body else heading_index + 1
            for index in range(first_index, min(len(paragraphs), heading_index + 4)):
                if index != heading_index and _heading_kind(paragraphs[index][2]):
                    break
                candidates.append((*paragraphs[index], "heading"))
        if not candidates:
            candidates.extend((*item, "keyword_fallback") for item in keyword_candidates[kind])

        selected_for_kind = 0
        per_kind_limit = max(1, max_passages // len(SECTION_KINDS))
        for start, end, paragraph, reason in candidates:
            if len(selected) >= max_passages or total >= max_total_chars:
                break
            if selected_for_kind >= per_kind_limit:
                break
            if (start, end) in used_ranges:
                continue
            allowance = min(max_section_chars - section_chars[kind], max_total_chars - total)
            if allowance <= 0:
                break
            excerpt = paragraph[:allowance]
            if not excerpt:
                continue
            passage = {
                "id": f"p{len(selected) + 1}", "section": kind,
                "start": start, "end": start + len(excerpt),
                "text": excerpt, "selection_reason": reason,
                "truncated": len(excerpt) < len(paragraph),
            }
            selected.append(passage)
            used_ranges.add((start, end))
            section_chars[kind] += len(excerpt)
            total += len(excerpt)
            selected_for_kind += 1

    sections = {}
    for kind in SECTION_KINDS:
        section_passages = [item for item in selected if item["section"] == kind]
        sections[kind] = {
            "state": "inspected_selected_passages" if section_passages else "not_inspected",
            "selected_chars": sum(len(item["text"]) for item in section_passages),
            "heading_found": bool(headings[kind]),
            # Selection does not establish whole-section absence. This flag is
            # true only when a complete selected paragraph fit the budget.
            "selected_passages_complete": bool(section_passages)
                                           and all(not item["truncated"] for item in section_passages),
        }
    return {
        "version": PASSAGE_VERSION,
        "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "source_chars": len(text), "selected_chars": total,
        "truncated": total < len(text), "passages": selected, "sections": sections,
    }


def normalize_assessment(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    contribution_type = str(raw.get("contribution_type") or "unknown").lower()
    claims = raw.get("claims") if isinstance(raw.get("claims"), dict) else {}
    normalized_claims = {}
    for kind in CLAIM_KINDS:
        claim = claims.get(kind) if isinstance(claims.get(kind), dict) else {}
        state = str(claim.get("state") or "unknown").lower()
        normalized_claims[kind] = {
            "state": state if state in CLAIM_STATES else "unknown",
            "citations": claim.get("citations") if isinstance(claim.get("citations"), list) else [],
            "rationale": str(claim.get("rationale") or "")[:500],
        }
    return {
        "contribution_type": contribution_type if contribution_type in CONTRIBUTION_TYPES else "unknown",
        "experimental_claims_made": bool(raw.get("experimental_claims_made")),
        "overclaim_risk": str(raw.get("overclaim_risk") or "unknown").lower(),
        "claims": normalized_claims,
    }


def _quote_supports_claim(kind: str, quote: str, section: str) -> bool:
    """Conservative lexical gate in addition to exact-location validation.

    This is not a semantic judge. It prevents a model from earning credit by
    citing a correctly located but obviously irrelevant sentence or a lone
    number/buzzword.
    """
    lowered = quote.lower()
    enough_context = len(re.findall(r"[a-z0-9]+", lowered)) >= 7
    if not enough_context:
        return False
    if kind == "measurements":
        measurement_denied = any(re.search(pattern, lowered) for pattern in (
            r"\b(no|without)\s+(?:any\s+)?(experiment|evaluation|measurement)s?\b",
            r"\b(did|does|do|could|can)\s+not\s+(conduct|perform|run|evaluate|measure)\b",
            r"\b(experiment|evaluation|measurement)s?\s+(?:was|were|is|are)?\s*not\s+"
            r"(conducted|performed|run|included|reported)\b",
            r"\bnot\s+(experimentally\s+)?(evaluated|measured|tested)\b",
        ))
        if measurement_denied:
            return False
        measurement = re.search(r"\b(measur|evaluat|experiment|accuracy|latency|throughput|error rate|sample|dataset)", lowered)
        result = re.search(r"\b\d+(?:\.\d+)?\s*(?:%|ms|s|seconds?|minutes?|x|times?)?\b", lowered)
        return section == "evaluation" and bool(measurement and result)
    if kind == "baselines":
        baseline_denied = any(re.search(pattern, lowered) for pattern in (
            r"\b(no|without)\s+(?:a|any)?\s*(baseline|comparison|comparator)s?\b",
            r"\b(did|does|do|could|can)\s+not\s+(compare|benchmark)\b",
            r"\b(baseline|comparison|comparator)s?\s+(?:was|were|is|are)?\s*not\s+"
            r"(used|included|reported|performed)\b",
            r"\bnot\s+compared\b",
        ))
        if baseline_denied:
            return False
        comparison = re.search(r"\b(compared|versus|vs\.?|outperform|underperform|relative to)\b", lowered)
        comparator = re.search(r"\b(baseline|existing|prior|state[- ]of[- ]the[- ]art|control)\b", lowered)
        return section == "evaluation" and bool(comparison and comparator)
    if kind == "reasoning":
        return section in {"method", "conclusion"} and bool(re.search(
            r"\b(because|therefore|so that|depends on|requires|enables|assumes|when|if)\b", lowered
        ))
    if kind == "tradeoffs":
        return section in {"method", "limitations", "conclusion"} and bool(re.search(
            r"\b(trade[- ]?off|however|at the cost|in exchange|while|but|constraint|tension)\b", lowered
        ))
    if kind == "actionable_insight":
        return section in {"method", "conclusion"} and bool(re.search(
            r"\b(should|must|recommend|adopt|implement|deploy|prioritize|step|practice)\b", lowered
        ))
    if kind == "limitations":
        return section == "limitations" and bool(re.search(
            r"\b(limit|cannot|does not|may not|threat|caveat|constraint|future work)\b", lowered
        ))
    return False


def validate_grounding(assessment: dict[str, Any], selection: dict[str, Any]) -> dict[str, Any]:
    """Resolve citations to exact selected text; unresolved citations earn no credit."""
    passages = {item["id"]: item for item in selection.get("passages", [])}
    normalized = normalize_assessment(assessment)
    for kind, claim in normalized["claims"].items():
        valid = []
        for citation in claim["citations"]:
            if not isinstance(citation, dict):
                continue
            passage = passages.get(str(citation.get("passage_id") or ""))
            quote = str(citation.get("quote") or "").strip()
            if (passage and quote and quote in passage["text"]
                    and _quote_supports_claim(kind, quote, passage["section"])):
                valid.append({
                    "passage_id": passage["id"], "quote": quote,
                    "start": passage["start"] + passage["text"].index(quote),
                    "end": passage["start"] + passage["text"].index(quote) + len(quote),
                })
        claim["valid_citations"] = valid
        claim["grounded"] = claim["state"] == "present" and bool(valid)
        if claim["state"] == "absent":
            # Absence is bounded to selected excerpts. It cannot describe the
            # whole paper when text was unavailable, omitted, or truncated.
            relevant = selection.get("sections", {}).get(
                "evaluation" if kind in {"measurements", "baselines"} else "method", {})
            claim["absence_scope"] = (
                "selected_passages" if relevant.get("selected_passages_complete") else "not_established"
            )
    return normalized


def evaluate_candidate_v4(
    candidate: dict[str, Any], profile: dict[str, Any], assessment: dict[str, Any],
    selection: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate an offline proposal while retaining V3 topical/profile semantics."""
    baseline = evaluate_candidate(candidate, profile)
    components = baseline["score_components"]
    grounded = validate_grounding(assessment, selection)
    claims = grounded["claims"]
    contribution = grounded["contribution_type"]

    topical_fit = min(85.0, components["topic"] + components["profile"])
    rigor = 0.0
    reasons: list[str] = []
    if contribution in {"empirical", "systems"}:
        if claims["measurements"]["grounded"]:
            rigor += 12.0
            reasons.append("grounded measurements")
        if claims["baselines"]["grounded"]:
            rigor += 6.0
            reasons.append("grounded baselines")
    elif contribution in {"architecture", "perspective", "framework", "theoretical", "survey"}:
        for kind, credit, label in (
            ("reasoning", 8.0, "grounded substantive reasoning"),
            ("tradeoffs", 6.0, "grounded tradeoffs"),
            ("actionable_insight", 5.0, "grounded actionable insight"),
            ("limitations", 3.0, "grounded limitations"),
        ):
            if claims[kind]["grounded"]:
                rigor += credit
                reasons.append(label)
    measurement_claim = claims["measurements"]
    unsupported_experiment = grounded["experimental_claims_made"] and (
        (measurement_claim["state"] == "present" and not measurement_claim["grounded"])
        or (measurement_claim["state"] == "absent"
            and measurement_claim.get("absence_scope") == "selected_passages")
    )
    if unsupported_experiment:
        rigor -= 12.0
        reasons.append("experimental claims lack grounded measurements in inspected passages")
    # Unknown, unavailable, or ungrounded assessments receive no positive credit.
    if grounded["overclaim_risk"] == "high":
        rigor -= 10.0
        reasons.append("high overclaim risk")
    elif grounded["overclaim_risk"] == "medium":
        rigor -= 4.0
        reasons.append("medium overclaim risk")

    preference_penalty = components["negative_penalty"]
    off_domain_penalty = components["off_domain_penalty"]
    score = round(max(0.0, min(100.0, topical_fit + rigor - preference_penalty - off_domain_penalty)), 2)
    no_rigor_score = round(max(0.0, min(100.0, topical_fit - preference_penalty - off_domain_penalty)), 2)
    return {
        **candidate, "score": score,
        "rationale": (
            f"Offline V4 proposal: topical fit {topical_fit:.1f}; contribution {contribution}; "
            f"grounded rigor {rigor:+.1f}; preference/off-domain penalties "
            f"-{preference_penalty + off_domain_penalty:.1f}. "
            + ("; ".join(reasons) if reasons else "No grounded contribution credit.")
        ),
        "score_components": {
            "version": SCORING_VERSION, "topical_fit": round(topical_fit, 3),
            "topic": components["topic"], "profile": components["profile"],
            "positive_matches": components["positive_matches"],
            "negative_matches": components["negative_matches"],
            "preference_penalty": preference_penalty,
            "off_domain_penalty": off_domain_penalty,
            "contribution_type": contribution, "grounded_rigor": rigor,
            "score_without_grounded_rigor": no_rigor_score,
            "assessment": grounded, "passage_selection": selection,
            "baseline_version": components["version"], "baseline_score": baseline["score"],
        },
    }
