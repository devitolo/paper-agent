from __future__ import annotations

import re
from typing import Any

ENGLISH_LANGUAGE_VALUES = {"en", "eng", "english", "en-us", "en-gb"}
RETRACTED_STATUS_VALUES = {"retracted", "withdrawn", "removed"}
NON_PAPER_TYPES = {
    "book-review", "book review", "cover", "contents", "editorial", "erratum", "index",
    "paratext", "reference-entry", "reference entry", "table-of-contents", "table of contents",
}
SURVEY_TYPES = {"review", "survey", "literature-review", "literature review"}
POSTER_TYPES = {"poster", "slides", "slide", "presentation", "presentation-slides"}

NON_PAPER_TITLE_PATTERNS = [
    r"\bcall for papers\b", r"\bcalls for papers\b", r"\btable of contents\b",
    r"\bfront matter\b", r"\bback matter\b", r"\berratum\b", r"\bcorrigendum\b",
    r"\badvertisement\b", r"\bbook review\b",
]
SURVEY_TITLE_PATTERNS = [
    r"\ba survey\b", r"\bsurvey of\b", r"\bsurvey on\b",
    r"\bsystematic literature review\b", r"\bliterature review\b", r"\bmapping study\b",
    r"\breview of\b",
]
BENCHMARK_TITLE_PATTERNS = [r"\bbenchmark\b", r"\bbenchmarks\b", r"\bbenchmarking\b"]
POSTER_TITLE_PATTERNS = [r"\bposter\b", r"\bslide deck\b", r"\bpresentation slides\b", r"\bworkshop presentation\b"]
RETRACTED_TITLE_PATTERNS = [
    r"^\s*\[?withdrawn\]?[:\s-]", r"^\s*\[?retracted\]?[:\s-]",
    r"\bretracted article\b", r"\bwithdrawn paper\b",
]


def _normalize(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _metadata_values(metadata: dict[str, Any]) -> list[str]:
    values: list[str] = []
    keys = (
        "type", "type_crossref", "document_type", "publication_type", "publicationTypes",
        "publication_types", "work_type", "subtype", "status", "language", "language_code",
    )
    containers = [metadata]
    if isinstance(metadata.get("source_metadata"), dict):
        containers.append(metadata["source_metadata"])
    for container in containers:
        for key in keys:
            value = container.get(key)
            if isinstance(value, list):
                values.extend(_normalize(item) for item in value)
            elif value is not None:
                values.append(_normalize(value))
    return [value for value in values if value]


def _metadata_language(metadata: dict[str, Any]) -> str | None:
    containers = [metadata]
    if isinstance(metadata.get("source_metadata"), dict):
        containers.append(metadata["source_metadata"])
    for container in containers:
        for key in ("language", "language_code"):
            value = container.get(key)
            if value:
                return _normalize(value)
    return None


def _matches_any(text: str, patterns: list[str]) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)


def _mostly_non_latin(text: str) -> bool:
    letters = [ch for ch in text if ch.isalpha()]
    if len(letters) < 24:
        return False
    latin = sum("a" <= ch.lower() <= "z" for ch in letters)
    return latin / len(letters) < 0.55


def curator_eligibility(candidate: dict[str, Any]) -> dict[str, Any]:
    """Return Curator-facing eligibility data without dropping Scout candidates."""
    title = _normalize(candidate.get("title"))
    abstract = _normalize(candidate.get("abstract"))
    metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
    metadata_values = set(_metadata_values(metadata))
    text = f"{title} {abstract}".strip()
    labels: list[str] = []
    disqualifier = None

    if metadata_values & RETRACTED_STATUS_VALUES or _matches_any(title, RETRACTED_TITLE_PATTERNS):
        disqualifier = "retracted_or_withdrawn"
    elif metadata_values & NON_PAPER_TYPES or _matches_any(title, NON_PAPER_TITLE_PATTERNS):
        disqualifier = "not_a_paper"

    if metadata_values & SURVEY_TYPES or _matches_any(title, SURVEY_TITLE_PATTERNS):
        labels.append("paper_type_survey")
    if _matches_any(title, BENCHMARK_TITLE_PATTERNS):
        labels.append("paper_type_benchmark")
    if metadata_values & POSTER_TYPES or _matches_any(title, POSTER_TITLE_PATTERNS):
        labels.append("paper_type_poster_or_slides")

    language = _metadata_language(metadata)
    if language and language not in ENGLISH_LANGUAGE_VALUES:
        labels.append("language_non_english")
    elif _mostly_non_latin(text):
        labels.append("language_non_english")

    return {
        "disqualified": disqualifier is not None,
        "disqualification_reason": disqualifier,
        "labels": labels,
    }
