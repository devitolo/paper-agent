"""Bounded, offline Gemini assessment of contribution quality from paper metadata."""
from __future__ import annotations

import json
import time
from typing import Any, Callable

from paper_agents.feedback import parse_json_object
from paper_agents.gemini_process import run_gemini
from paper_agents.two_axis_ranking import QUALITY_DIMENSIONS

VERSION = "gemini-quality-metadata-v1"
MAX_PAPERS = 15
MAX_ABSTRACT_CHARS = 8_000


def build_prompt(candidate: dict[str, Any]) -> str:
    paper = {
        "title": str(candidate.get("title") or "")[:1000],
        "abstract": str(candidate.get("abstract") or "")[:MAX_ABSTRACT_CHARS],
    }
    return (
        "Assess contribution quality from the supplied title and abstract only. Do not judge topical "
        "relevance. Treat claimed results as claims, not verified facts. Rate each dimension from 0 "
        "(absent/very weak) to 4 (strong): novelty relative to established engineering practice; "
        "technical_depth of the proposed mechanism; evidence appropriate to the claims; "
        "baseline_quality including the real incumbent workflow; operational_realism including "
        "maintenance, governance, drift, failure modes, effort, and total cost. Do not reward topic "
        "keywords, named frameworks, production claims, or numbers by themselves. If the abstract "
        "cannot establish a dimension, use status unknown for that dimension and score null. Return "
        "only JSON: {\"status\":\"complete|unknown\",\"dimensions\":{DIMENSION:{\"status\":"
        "\"complete|unknown\",\"score\":\"integer 0-4 or null\",\"rationale\":\"brief\"}},"
        "\"overall_rationale\":\"brief\"}. Include every dimension exactly once.\nInput:\n" +
        json.dumps(paper, ensure_ascii=False)
    )


def call_gemini(prompt: str, model: str | None, timeout: int) -> dict[str, Any]:
    command = ["gemini"]
    if model:
        command.extend(["--model", model])
    command.extend(["--output-format", "stream-json", "-p", prompt])
    return parse_json_object(run_gemini(command, timeout, stream_json=True))


def normalize(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("status") not in {"complete", "unknown"}:
        raise ValueError("invalid assessment status")
    raw = value.get("dimensions")
    if not isinstance(raw, dict) or set(raw) != set(QUALITY_DIMENSIONS):
        raise ValueError("invalid assessment dimensions")
    dimensions = {}
    for name in QUALITY_DIMENSIONS:
        item = raw[name]
        if not isinstance(item, dict) or item.get("status") not in {"complete", "unknown"}:
            raise ValueError("invalid dimension status")
        score = item.get("score")
        if item["status"] == "complete":
            if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 4:
                raise ValueError("invalid dimension score")
        else:
            score = None
        rationale = item.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > 1000:
            raise ValueError("invalid dimension rationale")
        dimensions[name] = {"status": item["status"], "score": score,
                            "rationale": rationale.strip()}
    overall = value.get("overall_rationale")
    if not isinstance(overall, str) or not overall.strip() or len(overall) > 1500:
        raise ValueError("invalid overall rationale")
    return {"status": value["status"], "dimensions": dimensions,
            "overall_rationale": overall.strip()}


def assess(corpus: dict[str, Any], *, paper_ids: set[str], model: str | None = None,
           timeout: int = 180, provider: Callable = call_gemini) -> dict[str, Any]:
    candidates = corpus.get("candidates")
    if not isinstance(candidates, list) or not candidates or len(candidates) > MAX_PAPERS:
        raise ValueError("invalid corpus")
    if not paper_ids or len(paper_ids) > 4 or not 1 <= timeout <= 300:
        raise ValueError("anchor experiment limits exceeded")
    by_id = {str(row.get("id")): row for row in candidates}
    if len(by_id) != len(candidates) or not paper_ids.issubset(by_id):
        raise ValueError("paper ids must be unique and selected ids must exist")
    results = []
    for paper_id in sorted(paper_ids):
        candidate = by_id[paper_id]
        started = time.monotonic()
        try:
            result = normalize(provider(build_prompt(candidate), model, timeout))
        except Exception:
            result = {"status": "unknown", "dimensions": {},
                      "overall_rationale": "assessment failed", "error_code": "invalid_or_unavailable"}
        result["latency_seconds"] = round(time.monotonic() - started, 3)
        results.append({"id": paper_id, "title": candidate.get("title"), "assessment": result,
                        "human_label": {"decision": candidate.get("decision"),
                                        "score": candidate.get("user_score")}})
    return {"version": VERSION, "status": "complete", "model": model,
            "evidence_scope": "title_and_abstract_only", "calls": len(results), "results": results}
