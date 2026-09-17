"""Read-only, deterministic replay for the offline ranking-quality proposal."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import time
from pathlib import Path
from typing import Any

from paper_agents.curator_quality_v4 import (
    CLAIM_KINDS, CLAIM_STATES, CONTRIBUTION_TYPES, PASSAGE_VERSION, SCORING_VERSION,
    evaluate_candidate_v4, select_targeted_passages, validate_grounding,
)
from paper_agents.curator_scoring import SCORING_VERSION as BASELINE_VERSION, evaluate_candidate


REPLAY_VERSION = "ranking-quality-replay-v1"
ASSESSMENT_SCHEMA_VERSION = 1
ASSESSMENT_STATUSES = {"complete"}
OVERCLAIM_RISKS = {"low", "medium", "high", "unknown"}


def paths_collide(first: Path, second: Path) -> bool:
    """Detect textual, symlink, and hard-link identity without creating paths."""
    try:
        return os.path.samefile(first, second)
    except FileNotFoundError:
        return first.resolve(strict=False) == second.resolve(strict=False)


def canonical_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def _assessment_contract(
    value: Any, selection: dict[str, Any],
) -> tuple[str, list[str], int]:
    """Classify stored assessor output without treating arbitrary JSON as a judgment."""
    if not value:
        return "missing", ["assessment is absent"], 0
    if not isinstance(value, dict):
        return "invalid", ["assessment must be an object"], 0
    errors = []
    if value.get("schema_version") != ASSESSMENT_SCHEMA_VERSION:
        errors.append(f"schema_version must be {ASSESSMENT_SCHEMA_VERSION}")
    if not isinstance(value.get("status"), str) or value.get("status") not in ASSESSMENT_STATUSES:
        errors.append("status must be complete")
    if (not isinstance(value.get("contribution_type"), str)
            or value.get("contribution_type") not in CONTRIBUTION_TYPES - {"unknown"}):
        errors.append("contribution_type must be a known contribution type")
    if not isinstance(value.get("experimental_claims_made"), bool):
        errors.append("experimental_claims_made must be boolean")
    if (not isinstance(value.get("overclaim_risk"), str)
            or value.get("overclaim_risk") not in OVERCLAIM_RISKS):
        errors.append("overclaim_risk must be low, medium, high, or unknown")
    claims = value.get("claims")
    if not isinstance(claims, dict):
        errors.append("claims must be an object")
        claims = {}
    passages = {str(item.get("id")): item for item in selection.get("passages", [])}
    usable_citations = 0
    for kind in CLAIM_KINDS:
        claim = claims.get(kind)
        if not isinstance(claim, dict):
            errors.append(f"claims.{kind} must be an object")
            continue
        if not isinstance(claim.get("state"), str) or claim.get("state") not in CLAIM_STATES:
            errors.append(f"claims.{kind}.state is invalid")
        citations = claim.get("citations")
        if not isinstance(citations, list):
            errors.append(f"claims.{kind}.citations must be a list")
            continue
        exact_count = 0
        for citation in citations:
            if not isinstance(citation, dict):
                continue
            passage = passages.get(str(citation.get("passage_id") or ""))
            quote = citation.get("quote")
            if passage and isinstance(quote, str) and quote.strip() and quote in passage.get("text", ""):
                exact_count += 1
        usable_citations += exact_count
        if claim.get("state") == "present" and exact_count == 0:
            errors.append(f"claims.{kind} is present without an exact selected-passage citation")
    return ("valid" if not errors else "invalid"), errors, usable_citations


def _validate_fixture(fixture: dict[str, Any]) -> None:
    if fixture.get("schema_version") != 1:
        raise ValueError("fixture schema_version must be 1")
    if not isinstance(fixture.get("profile"), dict):
        raise ValueError("fixture requires a frozen profile object")
    if fixture.get("profile_sha256") != canonical_hash(fixture["profile"]):
        raise ValueError("profile_sha256 does not match the frozen profile")
    candidates = fixture.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("fixture requires at least one candidate")
    ids = [str(item.get("id") or "") for item in candidates]
    if any(not item for item in ids) or len(ids) != len(set(ids)):
        raise ValueError("candidate ids must be non-empty and unique")
    if fixture.get("candidates_sha256") != canonical_hash(candidates):
        raise ValueError("candidates_sha256 does not match the frozen candidates")
    partitions = fixture.get("partitions")
    if not isinstance(partitions, dict):
        raise ValueError("fixture requires frozen partitions")
    assigned = [str(item) for values in partitions.values() for item in values]
    if sorted(assigned) != sorted(ids) or len(assigned) != len(set(assigned)):
        raise ValueError("each candidate must appear in exactly one partition")
    if partitions.get("unassigned"):
        raise ValueError("freeze every unassigned candidate before replay")
    budgets = fixture.get("budgets") or {}
    for field, maximum in (("max_total_chars", 7000), ("max_section_chars", 2000), ("max_passages", 8)):
        value = budgets.get(field)
        if not isinstance(value, int) or value < 1 or value > maximum:
            raise ValueError(f"{field} must be an integer from 1 to {maximum}")


def replay_fixture(fixture: dict[str, Any]) -> dict[str, Any]:
    """Replay stored inputs only. This function has no network or database path."""
    _validate_fixture(fixture)
    started = time.monotonic()
    profile = fixture["profile"]
    budgets = fixture["budgets"]
    assessment_records = []
    results = []
    for item in fixture["candidates"]:
        candidate_started = time.monotonic()
        candidate = dict(item)
        for replay_only in (
            "id", "document_text", "evidence_scope", "decision", "user_score", "proposed_assessment",
        ):
            candidate.pop(replay_only, None)
        baseline = evaluate_candidate(candidate, profile)
        selection = select_targeted_passages(
            str(item.get("document_text") or ""),
            max_total_chars=budgets["max_total_chars"],
            max_section_chars=budgets["max_section_chars"],
            max_passages=budgets["max_passages"],
        )
        assessment_status, assessment_errors, exact_citations = _assessment_contract(
            item.get("proposed_assessment"), selection,
        )
        grounded_assessment = validate_grounding(item.get("proposed_assessment") or {}, selection)
        grounded_citations = sum(
            len(claim.get("valid_citations", []))
            for claim in grounded_assessment.get("claims", {}).values()
            if claim.get("grounded")
        ) if assessment_status == "valid" else 0
        assessment_records.append({
            "id": str(item["id"]), "status": assessment_status,
            "errors": assessment_errors, "exact_reference_count": exact_citations,
            "grounded_usable_citations": grounded_citations,
            "evidence_scope": item.get("evidence_scope", "unavailable"),
        })
        proposed = evaluate_candidate_v4(
            candidate, profile,
            item.get("proposed_assessment") if assessment_status == "valid" else {},
            selection,
        )
        results.append({
            "id": str(item["id"]), "partition": next(
                name for name, ids in fixture["partitions"].items() if str(item["id"]) in {str(i) for i in ids}
            ),
            "title": item.get("title"), "decision": item.get("decision"),
            "user_score": item.get("user_score"),
            "evidence_scope": item.get("evidence_scope", "unavailable"),
            "proposed_assessment_status": assessment_status,
            "baseline_score": baseline["score"], "proposed_score": proposed["score"],
            "delta": round(proposed["score"] - baseline["score"], 2),
            "baseline_components": baseline["score_components"],
            "proposed_components": proposed["score_components"],
            "runtime_seconds": round(time.monotonic() - candidate_started, 6),
        })

    top_k = int(fixture.get("evaluation", {}).get("top_k", 3))
    useful_min_score = float(fixture.get("evaluation", {}).get("useful_min_score", 3))
    partition_reports = {}
    for partition in fixture["partitions"]:
        rows = [row for row in results if row["partition"] == partition]
        baseline_order = sorted(rows, key=lambda row: (-row["baseline_score"], row["id"]))
        proposed_order = sorted(rows, key=lambda row: (-row["proposed_score"], row["id"]))

        def metrics(order: list[dict[str, Any]]) -> dict[str, Any]:
            shortlisted = order[:top_k]
            rejections = [row for row in order if row["decision"] == "reject"]
            useful = [row for row in order if row["decision"] == "keep"
                      and isinstance(row["user_score"], (int, float))
                      and row["user_score"] >= useful_min_score]
            return {
                "order": [row["id"] for row in order],
                "high_ranked_rejections": [row["id"] for row in shortlisted if row["decision"] == "reject"],
                "missed_useful": [row["id"] for row in order[top_k:]
                                  if row["decision"] == "keep"
                                  and isinstance(row["user_score"], (int, float))
                                  and row["user_score"] >= useful_min_score],
                "labeled_count": sum(row["decision"] in {"keep", "reject"} for row in order),
                "shortlist_denominator": len(shortlisted),
                "rejection_denominator": len(rejections),
                "useful_denominator": len(useful),
            }
        partition_reports[partition] = {
            "count": len(rows), "baseline": metrics(baseline_order), "proposed": metrics(proposed_order),
        }

    runtimes = [row["runtime_seconds"] for row in results]
    negative_match_rows = [
        {"id": row["id"], "decision": row["decision"], "user_score": row["user_score"],
         "negative_matches": row["baseline_components"]["negative_matches"],
         "negative_penalty": row["baseline_components"]["negative_penalty"]}
        for row in results if row["baseline_components"]["negative_matches"]
    ]
    valid_ids = [row["id"] for row in assessment_records if row["status"] == "valid"]
    missing_ids = [row["id"] for row in assessment_records if row["status"] == "missing"]
    invalid_ids = [row["id"] for row in assessment_records if row["status"] == "invalid"]
    exact_reference_count = sum(row["exact_reference_count"] for row in assessment_records)
    usable_citation_count = sum(row["grounded_usable_citations"] for row in assessment_records)
    heldout_count = len(fixture["partitions"].get("heldout", []))
    evaluation_blockers = []
    if missing_ids:
        evaluation_blockers.append("missing_proposed_assessments")
    if invalid_ids:
        evaluation_blockers.append("invalid_proposed_assessments")
    if usable_citation_count == 0:
        evaluation_blockers.append("no_usable_assessment_citations")
    quality_claim_blockers = list(evaluation_blockers)
    if heldout_count == 0:
        quality_claim_blockers.append("no_heldout_candidates")
    quality_claim_blockers.append("external_quality_review_required")
    return {
        "status": "INCOMPLETE" if evaluation_blockers else "COMPLETE",
        "evaluation_complete": not evaluation_blockers,
        "quality_claim_ready": False,
        "quality_claim_blockers": quality_claim_blockers,
        "assessment_coverage": {
            "candidate_count": len(fixture["candidates"]),
            "supplied_count": len(fixture["candidates"]) - len(missing_ids),
            "valid_count": len(valid_ids),
            "invalid_count": len(invalid_ids),
            "missing_count": len(missing_ids),
            "valid_ids": valid_ids,
            "invalid_ids": invalid_ids,
            "missing_ids": missing_ids,
            "exact_reference_count": exact_reference_count,
            "usable_citation_count": usable_citation_count,
            "records": assessment_records,
            "note": (
                "Missing or invalid proposed assessments use a deterministic zero-rigor fallback. "
                "Their proposed scores are diagnostics, not a completed V4 comparison. Structural "
                "completion is separate from external quality approval."
            ),
        },
        "replay_version": REPLAY_VERSION,
        "corpus_id": fixture.get("corpus_id"),
        "corpus_sha256": canonical_hash(fixture["candidates"]),
        "profile_id": fixture.get("profile_id"), "profile_sha256": fixture["profile_sha256"],
        "baseline_version": BASELINE_VERSION, "proposed_version": SCORING_VERSION,
        "passage_version": PASSAGE_VERSION, "model_config": fixture.get("model_config"),
        "budgets": budgets, "evaluation": fixture.get("evaluation"),
        "partitions": partition_reports, "results": results,
        "runtime_seconds": round(time.monotonic() - started, 4),
        "runtime_distribution_seconds": {
            "minimum": min(runtimes), "median": statistics.median(runtimes), "maximum": max(runtimes),
        },
        "profile_interpretation_audit": {
            "negative_match_candidates": negative_match_rows,
            "kept_candidates_with_negative_matches": [
                row["id"] for row in negative_match_rows if row["decision"] == "keep"
            ],
            "note": "This audit reports current-profile matching; it does not modify or reinterpret the profile.",
        },
        "model_calls": 0, "network_calls": 0,
        "limitations": [
            "Stored judgments are replayed; this does not validate model judgment quality.",
            "Small or previously seen samples cannot establish broad ranking improvement.",
            "An INCOMPLETE report must not be used to claim proposed-ranking quality.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline contribution-aware ranking replay")
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output and paths_collide(args.fixture, args.output):
        parser.error("--output must not overwrite the frozen fixture")
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    report = replay_fixture(fixture)
    output = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")


if __name__ == "__main__":
    main()
