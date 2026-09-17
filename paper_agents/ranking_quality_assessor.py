"""Bounded, label-isolated local assessor for offline ranking experiments."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
import subprocess
import tempfile
import time
import urllib.request
from itertools import zip_longest
from pathlib import Path
from typing import Any, Callable

from paper_agents.curator_quality_v4 import CLAIM_KINDS, select_targeted_passages
from paper_agents.local_extract import DEFAULT_MODEL, DEFAULT_OLLAMA_URL
from paper_agents.ranking_quality_replay import _assessment_contract, _validate_fixture, canonical_hash


ASSESSOR_VERSION = "contribution-assessor-v1"
MAX_OUTPUT_TOKENS = 1200


def call_assessor_ollama(url: str, model: str, prompt: str, timeout: int) -> dict[str, Any]:
    payload = {
        "model": model, "prompt": prompt, "format": "json", "stream": False,
        "options": {"temperature": 0, "num_predict": MAX_OUTPUT_TOKENS},
    }
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.load(response)
    if not isinstance(result, dict):
        raise ValueError("model response envelope is not an object")
    return result


def _profile_focus(profile: dict[str, Any]) -> dict[str, list[str]]:
    """Expose explicit focus fields only; notes and review history stay out of prompts."""
    result = {}
    for key in ("interests", "positive_signals", "negative_signals"):
        values = profile.get(key)
        result[key] = [item[:500] for item in values if isinstance(item, str)][:20] \
            if isinstance(values, list) else []
    return result


def build_assessment_prompt(
    candidate: dict[str, Any], profile: dict[str, Any], selection: dict[str, Any],
) -> str:
    """Build a prompt from an allowlist that excludes decisions, scores and reviews."""
    schema = {
        "schema_version": 1, "status": "complete",
        "contribution_type": "empirical|systems|architecture|perspective|framework|survey|theoretical",
        "experimental_claims_made": "boolean", "overclaim_risk": "low|medium|high|unknown",
        "claims": {kind: {"state": "present|absent|unknown", "citations": [
            {"passage_id": "p1", "quote": "exact substring from that passage"}
        ]} for kind in CLAIM_KINDS},
    }
    passages = [{"id": row["id"], "section": row["section"], "text": row["text"]}
                for row in selection.get("passages", [])]
    payload = {
        "title": str(candidate.get("title") or "")[:500],
        "profile_focus": _profile_focus(profile),
        "evidence_scope": "primary_pdf_selected_passages",
        "passages": passages,
    }
    return (
        "You are a conservative research assessor. Use only the supplied primary-paper passages. "
        "Return exactly one JSON object matching this schema: "
        + json.dumps(schema, separators=(",", ":"))
        + ". Every present claim requires an exact quote copied from its cited passage. "
        "Use unknown when the selected passages do not establish a claim. Do not infer missing "
        "experiments, baselines, deployment, or limitations.\nInput:\n"
        + json.dumps(payload, ensure_ascii=False)
    )


def _pdf_paths(db_path: Path, ids: list[str]) -> dict[str, Path]:
    uri = f"file:{db_path.resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        rows = connection.execute(
            """SELECT paper_id, path FROM artifacts
               WHERE artifact_type='pdf' AND paper_id IN (%s)
               ORDER BY id DESC""" % ",".join("?" for _ in ids),
            [int(item) for item in ids],
        ).fetchall() if ids else []
    finally:
        connection.close()
    result: dict[str, Path] = {}
    for paper_id, path in rows:
        result.setdefault(str(paper_id), Path(path))
    return result


def _read_primary_text(
    source: Path, max_chars: int, *, pdf_layout: str | None = "two-column-a4",
) -> tuple[str, bool]:
    """Read at most max_chars+1 and bound PDF conversion time."""
    if source.suffix.lower() != ".pdf":
        with source.open("r", encoding="utf-8", errors="ignore") as stream:
            text = stream.read(max_chars + 1)
        return text[:max_chars], len(text) > max_chars
    info = subprocess.run(
        ["pdfinfo", str(source)], check=True, capture_output=True, text=True, timeout=10,
    )
    if pdf_layout != "two-column-a4":
        raise ValueError("unsupported or unverified PDF layout")
    if isinstance(info, subprocess.CompletedProcess):
        size = re.search(r"Page size:\s+([0-9.]+)\s+x\s+([0-9.]+)\s+pts", info.stdout or "")
        if not size or not (580 <= float(size.group(1)) <= 610 and 825 <= float(size.group(2)) <= 855):
            raise ValueError("declared two-column-a4 PDF does not have an A4 portrait page size")
    with tempfile.TemporaryDirectory() as directory:
        left_path = Path(directory) / "left.txt"
        right_path = Path(directory) / "right.txt"
        for start, width, destination in ((0, 298, left_path), (298, 298, right_path)):
            subprocess.run(
                ["pdftotext", "-layout", "-r", "72", "-x", str(start), "-W", str(width),
                 str(source), str(destination)],
                check=True, capture_output=True, text=True, timeout=30,
            )
        with left_path.open("r", encoding="utf-8", errors="ignore") as stream:
            left = stream.read(max_chars + 1)
        with right_path.open("r", encoding="utf-8", errors="ignore") as stream:
            right = stream.read(max_chars + 1)
        left_pages, right_pages = left.split("\f"), right.split("\f")
        text = "\n\n".join(
            _mark_section_headings(left_page) + "\n\n" + _mark_section_headings(right_page)
            for left_page, right_page in zip_longest(left_pages, right_pages, fillvalue="")
        )
        mismatched_pages = len(left_pages) != len(right_pages)
        text = _strip_front_matter(text)
        truncated = (len(left) > max_chars or len(right) > max_chars
                     or len(text) > max_chars or mismatched_pages)
        return text[:max_chars], truncated


def _mark_section_headings(text: str) -> str:
    """Create paragraph boundaries around explicit section headings."""
    heading = re.compile(
        r"^(?:[IVXLCDM]+\.|[A-Z]\.)?\s*(?:method|approach|design|architecture|implementation|"
        r"system|evaluation|experiment|results|benchmark|measurement|study|limitations?|"
        r"threats?|caveats?|constraints?|future work|conclusion|discussion|implications?|"
        r"recommendations?)\b", re.IGNORECASE,
    )

    def paragraphs(lines: list[str]) -> str:
        output = []
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped[0].islower() and heading.match(stripped):
                output.extend(["", stripped, ""])
            else:
                output.append(line.rstrip())
        return "\n".join(output).strip()

    return paragraphs(text.splitlines())


def _strip_front_matter(text: str) -> str:
    """Exclude title/abstract/disclosures when a numbered paper body is detectable."""
    body = re.search(r"(?m)^\s*I\.\s+[A-Z]", text)
    return text[body.start():] if body else text


def _usage_count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def assess_fixture(
    fixture: dict[str, Any], db_path: Path, *, max_papers: int = 3,
    max_source_chars: int = 250_000, timeout: int = 90,
    model: str = DEFAULT_MODEL, ollama_url: str = DEFAULT_OLLAMA_URL,
    provider: Callable[[str, str, str, int], dict[str, Any]] = call_assessor_ollama,
    pdf_layouts: dict[str, str] | None = None,
    primary_paths: dict[str, Path] | None = None, conversion_only: bool = False,
    target_ids: set[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Assess available primary PDFs and return a new fixture; never writes the DB."""
    if not 1 <= max_papers <= 15:
        raise ValueError("max_papers must be from 1 to 15")
    if not 1 <= timeout <= 180:
        raise ValueError("timeout must be from 1 to 180 seconds")
    if not 10_000 <= max_source_chars <= 500_000:
        raise ValueError("max_source_chars must be from 10000 to 500000")
    _validate_fixture(fixture)
    output = copy.deepcopy(fixture)
    pdf_layouts = pdf_layouts or {}
    primary_paths = primary_paths or {}
    target_ids = {str(item) for item in target_ids} if target_ids else None
    candidates = output.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("fixture requires candidates")
    ids = [str(item.get("id") or "") for item in candidates]
    if target_ids and not target_ids.issubset(set(ids)):
        raise ValueError("every target paper id must exist in the frozen fixture")
    paths = _pdf_paths(db_path, ids)
    paths.update({str(paper_id): Path(path) for paper_id, path in primary_paths.items()})
    report: dict[str, Any] = {
        "assessor_version": ASSESSOR_VERSION, "model": model,
        "limits": {"max_papers": max_papers, "max_source_chars": max_source_chars,
                   "max_selected_chars": output["budgets"]["max_total_chars"],
                   "max_output_tokens_per_call": MAX_OUTPUT_TOKENS,
                   "timeout_seconds_per_call": timeout, "retries": 0, "concurrency": 1},
        "candidate_count": len(candidates), "pdf_artifact_count": len(paths),
        "mode": "conversion_only" if conversion_only else "assessment",
        "attempted": 0, "converted": 0, "completed": 0, "skipped": [], "failures": [],
        "model_calls": 0, "prompt_chars": 0, "reported_prompt_tokens": None,
        "reported_output_tokens": None, "elapsed_seconds": 0.0, "assessments": [],
        "label_isolation": "prompt allowlist excludes decision, user_score, feedback, and reviews",
        "pdf_layout_policy": "PDFs require an explicit supported per-paper layout declaration",
        "conversion_reviews": [],
    }
    started = time.monotonic()
    for candidate in candidates:
        paper_id = str(candidate["id"])
        # Every result belongs to this assessor run. Never retain a stale
        # successful judgment when a reassessment is skipped or fails.
        candidate["proposed_assessment"] = {}
        if target_ids and paper_id not in target_ids:
            report["skipped"].append({"id": paper_id, "reason": "not_targeted"})
            continue
        if report["attempted"] >= max_papers:
            report["skipped"].append({"id": paper_id, "reason": "paper_budget"})
            continue
        source = paths.get(paper_id)
        if source is None or not source.is_file():
            report["skipped"].append({"id": paper_id, "reason": "primary_pdf_unavailable"})
            continue
        report["attempted"] += 1
        try:
            primary_text, source_truncated = _read_primary_text(
                source, max_source_chars, pdf_layout=pdf_layouts.get(paper_id),
            )
            selection = select_targeted_passages(primary_text, **{
                "max_total_chars": output["budgets"]["max_total_chars"],
                "max_section_chars": output["budgets"]["max_section_chars"],
                "max_passages": output["budgets"]["max_passages"],
            })
            if not selection["passages"]:
                raise ValueError("no targeted primary-text passages selected")
            report["converted"] += 1
            report["conversion_reviews"].append({
                "id": paper_id,
                "primary_text_sha256": hashlib.sha256(primary_text.encode()).hexdigest(),
                "source_chars_read": len(primary_text), "source_truncated": source_truncated,
                "selected_chars": selection["selected_chars"], "sections": selection["sections"],
                "passages": [{key: passage[key] for key in (
                    "id", "section", "start", "end", "selection_reason", "truncated", "text"
                )} for passage in selection["passages"]],
            })
            candidate["document_text"] = primary_text
            candidate["evidence_scope"] = "primary_pdf_text"
            if conversion_only:
                continue
            prompt = build_assessment_prompt(candidate, output["profile"], selection)
            report["prompt_chars"] += len(prompt)
            report["model_calls"] += 1
            envelope = provider(ollama_url, model, prompt, timeout)
            response = envelope.get("response") if isinstance(envelope, dict) else None
            assessment = json.loads(response) if isinstance(response, str) else None
            status, errors, _ = _assessment_contract(assessment, selection)
            if status != "valid":
                raise ValueError("invalid assessment: " + "; ".join(errors))
            assessment["assessor_provenance"] = {
                "version": ASSESSOR_VERSION, "model": model,
                "primary_text_sha256": hashlib.sha256(primary_text.encode()).hexdigest(),
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                "response_sha256": hashlib.sha256(response.encode()).hexdigest(),
                "evidence_scope": "primary_pdf_selected_passages",
                "pdf_layout": pdf_layouts.get(paper_id),
                "source_chars_read": len(primary_text), "source_truncated": source_truncated,
                "selected_chars": selection["selected_chars"],
            }
            candidate["proposed_assessment"] = assessment
            report["completed"] += 1
            prompt_tokens = _usage_count(envelope.get("prompt_eval_count"))
            output_tokens = _usage_count(envelope.get("eval_count"))
            if prompt_tokens is not None:
                report["reported_prompt_tokens"] = (
                    (report["reported_prompt_tokens"] or 0) + prompt_tokens
                )
            if output_tokens is not None:
                report["reported_output_tokens"] = (
                    (report["reported_output_tokens"] or 0) + output_tokens
                )
            report["assessments"].append({
                "id": paper_id, **assessment["assessor_provenance"],
            })
        except (OSError, RuntimeError, TimeoutError, subprocess.SubprocessError,
                ValueError, json.JSONDecodeError) as exc:
            if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)):
                code = "timeout"
            elif isinstance(exc, (json.JSONDecodeError, ValueError)):
                code = "invalid_assessment"
            elif isinstance(exc, subprocess.SubprocessError):
                code = "text_extraction_failed"
            else:
                code = "provider_or_source_error"
            report["failures"].append({"id": paper_id, "error_code": code})
    output["candidates_sha256"] = canonical_hash(output["candidates"])
    output["model_config"] = {
        "mode": "conversion_only" if conversion_only else "local_ollama_assessment", "model": model,
        "assessor_version": ASSESSOR_VERSION, "calls": report["model_calls"],
        "labels_excluded_from_prompt": True,
    }
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return output, report
