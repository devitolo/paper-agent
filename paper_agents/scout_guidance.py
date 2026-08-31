from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from paper_agents import db
from paper_agents.scout import count_phrase, normalize_text

DEFAULT_RECENT_FEEDBACK_LIMIT = 12
MAX_GUIDED_TOPICS = 8

GUIDANCE_TERM_BANK = [
    "aiops",
    "it operations",
    "llm",
    "agentic",
    "autonomous",
    "incident response",
    "incident management",
    "root cause analysis",
    "root-cause analysis",
    "failure diagnosis",
    "anomaly detection",
    "remediation",
    "mitigation",
    "observability",
    "telemetry",
    "log analysis",
    "trace analysis",
    "monitoring",
    "debugging",
    "automated debugging",
    "program repair",
    "software maintenance",
    "software reliability",
    "cloud operations",
    "microservice",
    "distributed systems",
    "production engineering",
    "developer productivity",
    "human-ai collaboration",
    "rag",
    "retrieval",
    "context grounding",
    "empirical",
    "weak evidence",
    "illustrative",
    "toy benchmark",
    "unrealistic",
    "real-world",
    "production",
]


@dataclass(frozen=True)
class ScoutGuidance:
    include_terms: list[str] = field(default_factory=list)
    boost_terms: list[str] = field(default_factory=list)
    avoid_terms: list[str] = field(default_factory=list)
    source_notes: list[str] = field(default_factory=list)
    feedback_count: int = 0
    profile_version_id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def has_terms(self) -> bool:
        return bool(self.include_terms or self.boost_terms or self.avoid_terms)


def load_scout_guidance(
    connection,
    *,
    feedback_limit: int = DEFAULT_RECENT_FEEDBACK_LIMIT,
) -> ScoutGuidance:
    profile_version = db.current_profile_version(connection)
    feedback_rows = db.recent_structured_feedback_with_papers(connection, limit=feedback_limit)
    return build_scout_guidance(profile_version, feedback_rows)


def build_scout_guidance(
    profile_version: dict[str, Any] | None,
    feedback_rows: list[dict[str, Any]],
) -> ScoutGuidance:
    profile = profile_version["profile"] if profile_version else {}
    include_terms: list[str] = []
    boost_terms: list[str] = []
    avoid_terms: list[str] = []
    source_notes: list[str] = []

    for term in profile.get("interests") or []:
        add_guidance_term(include_terms, term)
    for term in profile.get("positive_signals") or []:
        add_guidance_term(include_terms, term)
    for term in profile.get("negative_signals") or []:
        add_guidance_term(avoid_terms, term)

    for row in feedback_rows:
        decision = normalize_text(str(row.get("decision") or ""))
        score = row.get("score")
        try:
            numeric_score = float(score) if score is not None else None
        except (TypeError, ValueError):
            numeric_score = None
        text = feedback_row_text(row)
        terms = extract_guidance_terms(text)
        if decision == "keep" and (numeric_score is None or numeric_score >= 4):
            for term in terms:
                add_guidance_term(boost_terms, term)
            for signal in row.get("preference_signals") or []:
                add_guidance_term(boost_terms, signal)
            if row.get("source"):
                note = f"Recent high-score keep feedback came from {row['source']}."
                if note not in source_notes:
                    source_notes.append(note)
        elif decision == "reject" and (numeric_score is None or numeric_score <= 2):
            for term in terms:
                if term not in {"empirical", "real-world", "production"}:
                    add_guidance_term(avoid_terms, term)
            for signal in row.get("preference_signals") or []:
                add_guidance_term(avoid_terms, signal)

    return ScoutGuidance(
        include_terms=include_terms[:12],
        boost_terms=boost_terms[:8],
        avoid_terms=avoid_terms[:10],
        source_notes=source_notes[:4],
        feedback_count=len(feedback_rows),
        profile_version_id=profile_version["id"] if profile_version else None,
    )


def guidance_text(guidance: ScoutGuidance) -> str:
    parts = []
    if guidance.boost_terms:
        parts.append("Boost searches toward: " + ", ".join(guidance.boost_terms[:8]) + ".")
    if guidance.include_terms:
        parts.append("Keep searches anchored on: " + ", ".join(guidance.include_terms[:8]) + ".")
    if guidance.avoid_terms:
        parts.append("Penalize candidates matching: " + ", ".join(guidance.avoid_terms[:8]) + ".")
    if not parts:
        parts.append("No feedback-derived Scout guidance yet.")
    return " ".join(parts)


def guidance_summary(guidance: ScoutGuidance) -> dict[str, Any]:
    return {
        "profile_version_id": guidance.profile_version_id,
        "feedback_count": guidance.feedback_count,
        "include_terms": guidance.include_terms[:8],
        "boost_terms": guidance.boost_terms[:8],
        "avoid_terms": guidance.avoid_terms[:8],
        "source_notes": guidance.source_notes,
    }


def topics_with_guidance(topics: list[str], guidance: ScoutGuidance, *, max_topics: int = MAX_GUIDED_TOPICS) -> list[str]:
    base_topics = [topic.strip() for topic in topics if topic.strip()]
    guided = list(base_topics)
    for term in guidance.boost_terms[:3]:
        if len(guided) >= max_topics:
            break
        if not any(count_phrase(normalize_text(topic), term) for topic in guided):
            guided.append(term)
    for term in guidance.include_terms[:2]:
        if len(guided) >= max_topics:
            break
        if not any(count_phrase(normalize_text(topic), term) for topic in guided):
            guided.append(term)
    return guided or topics


def apply_guidance_to_candidates(
    candidates: list[Any],
    guidance: ScoutGuidance,
) -> list[tuple[Any, dict[str, Any]]]:
    annotated: list[tuple[int, int, Any, dict[str, Any]]] = []
    for index, candidate in enumerate(candidates):
        text = normalize_text(
            " ".join(
                [
                    getattr(candidate, "title", "") or "",
                    getattr(candidate, "abstract", "") or "",
                    " ".join(getattr(candidate, "categories", []) or []),
                    " ".join((getattr(candidate, "metadata", {}) or {}).get("keywords", []) or []),
                ]
            )
        )
        boost_hits = matching_terms(text, guidance.boost_terms + guidance.include_terms)
        avoid_hits = matching_terms(text, guidance.avoid_terms)
        excluded = len(avoid_hits) >= 2 and not boost_hits
        diagnostics = {
            "feedback_boost_hits": boost_hits,
            "feedback_avoid_hits": avoid_hits,
            "feedback_guidance_penalty": bool(avoid_hits),
            "feedback_guidance_excluded": excluded,
        }
        penalty_rank = len(avoid_hits) * 10 - len(boost_hits)
        annotated.append((1 if excluded else 0, penalty_rank, index, candidate, diagnostics))
    annotated.sort(key=lambda item: (item[0], item[1], item[2]))
    return [(candidate, diagnostics) for _, _, _, candidate, diagnostics in annotated]


def matching_terms(text: str, terms: list[str]) -> list[str]:
    matches: list[str] = []
    for term in terms:
        normalized = normalize_text(term)
        if normalized and count_phrase(text, normalized) and term not in matches:
            matches.append(term)
    return matches


def feedback_row_text(row: dict[str, Any]) -> str:
    observations = row.get("observations") or []
    signals = row.get("preference_signals") or []
    parts = [
        row.get("title") or "",
        row.get("abstract") or "",
        " ".join(row.get("categories") or []),
        " ".join(str(item) for item in observations),
        " ".join(str(item) for item in signals),
    ]
    return normalize_text(" ".join(parts))


def extract_guidance_terms(text: str) -> list[str]:
    terms: list[str] = []
    for term in GUIDANCE_TERM_BANK:
        if count_phrase(text, term):
            add_guidance_term(terms, term)
    return terms


def add_guidance_term(terms: list[str], value: Any) -> None:
    term = normalize_guidance_term(value)
    if term and term not in terms:
        terms.append(term)


def normalize_guidance_term(value: Any) -> str:
    text = normalize_text(str(value or ""))
    if not text or len(text) < 3:
        return ""
    return text[:80]
