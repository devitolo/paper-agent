"""Bounded local evidence assessment used by Curator V3."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from paper_agents.local_extract import DEFAULT_MODEL, DEFAULT_OLLAMA_URL, call_ollama

EVIDENCE_VERSION = "qwen-evidence-v1"
RESEARCH_TYPES = {"empirical", "systems", "theoretical", "survey", "position", "framework", "unknown"}
EVIDENCE_QUALITIES = {"strong", "moderate", "weak", "none", "unknown"}
OVERCLAIM_RISKS = {"low", "medium", "high", "unknown"}
CONFIDENCES = {"low", "medium", "high"}
REQUIRED_EVIDENCE_FIELDS = (
    "research_type", "experiment_or_evaluation", "real_data_or_deployment",
    "implementation_detail", "novelty", "evidence_quality", "overclaim_risk",
    "confidence", "rationale",
)


def _text_from_triage(path: str | None) -> str:
    if not path:
        return ""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    merged = data.get("merged", data)
    if not isinstance(merged, dict):
        return ""
    return " ".join(str(merged.get(key) or "") for key in (
        "research_problem", "why_it_matters", "approach", "paper_date",
    )).strip()


def evidence_input(candidate: dict[str, Any], max_chars: int) -> tuple[str, str]:
    provenance = candidate.get("evidence") or {}
    triage = _text_from_triage(provenance.get("triage_path"))
    if triage and provenance.get("full_text_triage"):
        return triage[:max_chars], "full_text_extraction"
    abstract = str(candidate.get("abstract") or "").strip()
    return abstract[:max_chars], "source_abstract" if abstract else "metadata_only"


def _prompt(title: str, text: str, provenance: str) -> str:
    schema = {
        "research_type": "empirical|systems|theoretical|survey|position|framework|unknown",
        "experiment_or_evaluation": "string",
        "real_data_or_deployment": "string",
        "implementation_detail": "string",
        "novelty": "string",
        "evidence_quality": "strong|moderate|weak|none|unknown",
        "overclaim_risk": "low|medium|high|unknown",
        "confidence": "low|medium|high",
        "rationale": "string",
    }
    return (
        "You are a conservative research-evidence reviewer. Return only one JSON object matching this "
        "schema: " + json.dumps(schema, separators=(",", ":")) + "\n"
        "Distinguish claims from demonstrated evidence. Do not infer experiments, implementation, datasets, "
        "or deployment that the supplied text does not state. Mark qualitative-only, synthetic-only, absent "
        "measurements, or unsupported broad claims clearly.\n"
        f"Title: {title}\nEvidence provenance: {provenance}\nSupplied text:\n{text}"
    )


def _normalize(value: Any, provenance: str, *, status: str = "ok", error: str | None = None) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    research_type = str(raw.get("research_type") or "unknown").lower()
    quality = str(raw.get("evidence_quality") or "unknown").lower()
    overclaim = str(raw.get("overclaim_risk") or "unknown").lower()
    confidence = str(raw.get("confidence") or "low").lower()
    return {
        "version": EVIDENCE_VERSION, "status": status, "error": error,
        "provenance": provenance,
        "research_type": research_type if research_type in RESEARCH_TYPES else "unknown",
        "experiment_or_evaluation": str(raw.get("experiment_or_evaluation") or "not stated"),
        "real_data_or_deployment": str(raw.get("real_data_or_deployment") or "not stated"),
        "implementation_detail": str(raw.get("implementation_detail") or "not stated"),
        "novelty": str(raw.get("novelty") or "not stated"),
        "evidence_quality": quality if quality in EVIDENCE_QUALITIES else "unknown",
        "overclaim_risk": overclaim if overclaim in OVERCLAIM_RISKS else "unknown",
        "confidence": confidence if confidence in CONFIDENCES else "low",
        "rationale": str(raw.get("rationale") or "No usable evidence assessment.")[:500],
    }


def _validate_response(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        raise ValueError("model response is not a populated JSON object")
    missing = [field for field in REQUIRED_EVIDENCE_FIELDS
               if not isinstance(value.get(field), str) or not value[field].strip()]
    if missing:
        raise ValueError("model response has invalid required evidence fields: " + ", ".join(missing))
    if value["research_type"].lower() not in RESEARCH_TYPES:
        raise ValueError("model response has an invalid research_type")
    if value["evidence_quality"].lower() not in EVIDENCE_QUALITIES:
        raise ValueError("model response has an invalid evidence_quality")
    if value["overclaim_risk"].lower() not in OVERCLAIM_RISKS:
        raise ValueError("model response has an invalid overclaim_risk")
    if value["confidence"].lower() not in CONFIDENCES:
        raise ValueError("model response has an invalid confidence")
    return value


def assess_evidence(candidate: dict[str, Any], *, model: str = DEFAULT_MODEL,
                    ollama_url: str = DEFAULT_OLLAMA_URL, timeout: int = 45,
                    max_chars: int = 7000) -> dict[str, Any]:
    started = time.monotonic()
    text, provenance = evidence_input(candidate, max_chars)
    if not text:
        assessment = _normalize({}, provenance, status="unavailable", error="no paper text available")
        assessment["wall_clock_sec"] = round(time.monotonic() - started, 2)
        assessment["model"] = model
        return assessment
    try:
        raw = call_ollama(ollama_url, model, _prompt(str(candidate.get("title") or ""), text, provenance), timeout)
        if not isinstance(raw, dict):
            raise ValueError("model response envelope is not an object")
        response = raw.get("response")
        if not isinstance(response, str):
            raise ValueError("model response is missing text")
        parsed = json.loads(response)
        assessment = _normalize(_validate_response(parsed), provenance)
    except (OSError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        assessment = _normalize({}, provenance, status="unavailable", error=str(exc))
    assessment["wall_clock_sec"] = round(time.monotonic() - started, 2)
    assessment["model"] = model
    return assessment
