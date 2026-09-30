"""Frozen MiniLM passage retrieval and optional Qwen summarization experiment."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from paper_agents.minilm_eval import MiniLMScorer
from paper_agents.summary_quality_experiment import (
    SECTION_FIELDS,
    call_experiment_ollama,
    source_context,
)


EXPERIMENT_VERSION = "minilm-section-retrieval-v1"
FIELD_QUERIES = {
    "research_problem": (
        "What concrete limitation, failure, operational problem, research gap, or unmet need "
        "does this paper address?"
    ),
    "why_it_matters": (
        "Who or what is affected by the problem, and what practical or scientific consequence "
        "makes solving it important?"
    ),
    "approach": (
        "What did the authors build, test, measure, or analyze, and how does the main method work?"
    ),
}
FIELD_HINTS = {
    "research_problem": {
        "challenge", "difficult", "failure", "gap", "inaccurate", "lack", "limitation",
        "manual", "missing", "overhead", "problem", "risk", "slow", "unreliable",
    },
    "why_it_matters": {
        "availability", "consequence", "cost", "downtime", "impact", "important",
        "operator", "performance", "recovery", "reliability", "risk", "service", "user",
    },
    "approach": {
        "algorithm", "analyze", "architecture", "build", "evaluate", "framework", "integrate",
        "measure", "method", "model", "pipeline", "system", "train", "use",
    },
}


def split_source_passages(source_text: str, *, max_chars: int = 900) -> list[str]:
    """Create overlapping sentence windows while preserving source wording."""
    normalized = re.sub(r"[ \t]+", " ", source_text.replace("\r", "\n"))
    normalized = re.sub(r"\n{2,}", "\n", normalized).strip()
    sentences = [
        item.strip()
        for item in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])|\n+", normalized)
        if item.strip()
    ]
    passages: list[str] = []
    for index, sentence in enumerate(sentences):
        passage = sentence
        next_index = index + 1
        while next_index < len(sentences) and len(passage) + 1 + len(sentences[next_index]) <= max_chars:
            passage += " " + sentences[next_index]
            next_index += 1
            if next_index - index >= 3:
                break
        if len(passage) >= 40 and passage not in passages:
            passages.append(passage)
    return passages


def rank_passages(
    passages: list[str],
    *,
    scorer: Any,
    top_k: int = 3,
    candidate_limit: int = 48,
) -> dict[str, list[dict[str, Any]]]:
    if top_k < 1:
        raise ValueError("top_k must be positive")
    ranked: dict[str, list[dict[str, Any]]] = {}
    for field, query in FIELD_QUERIES.items():
        hints = FIELD_HINTS[field]
        lexical = {
            index: len(hints & set(re.findall(r"[a-z]+", passage.casefold())))
            for index, passage in enumerate(passages, 1)
        }
        candidates = sorted(
            enumerate(passages, 1),
            key=lambda item: (-lexical[item[0]], item[0]),
        )[:candidate_limit]
        rows = []
        for index, passage in candidates:
            score = float(scorer.score_pair(query, passage))
            if not math.isfinite(score):
                raise RuntimeError("MiniLM returned a non-finite passage score")
            rows.append({"passage_id": f"p{index}", "score": score, "text": passage})
        ranked[field] = sorted(rows, key=lambda row: (-row["score"], row["passage_id"]))[:top_k]
    return ranked


def build_qwen_retrieval_prompt(title: str, ranked: dict[str, list[dict[str, Any]]]) -> str:
    candidates = {
        field: [
            {"passage_id": row["passage_id"], "text": row["text"]}
            for row in rows
        ]
        for field, rows in ranked.items()
    }
    schema: dict[str, str] = {}
    for field in SECTION_FIELDS:
        schema[field] = "one concise sentence|string|null"
        schema[f"{field}_passage_id"] = "listed passage_id|string|null"
    return f"""Write decision-useful research-paper fields from retrieved source passages.
Return exactly one JSON object matching this schema:
{json.dumps(schema, separators=(",", ":"))}

Use only the passages listed for each field. For every non-null field, select the single passage_id that best supports the sentence. Do not quote or rewrite evidence; the application copies the selected passage directly. Return null for both values if the listed passages do not support the field.

research_problem must name the affected system or area and a concrete limitation, failure, gap, or unmet need. Do not repeat the title.
why_it_matters must name the affected party or outcome and a concrete practical or scientific consequence. Do not describe the paper or framework.
approach must explain what was built, tested, measured, or analyzed and how the method works. A framework name, acronym, or capability list is not a mechanism.

Do not invent details, use promotional language, modify passage text, or include markdown or extra keys.

Title: {title}

RETRIEVED PASSAGES
{json.dumps(candidates, indent=2, ensure_ascii=False)}"""


def parse_qwen_retrieval_output(
    response_text: str,
    ranked: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, str | None], dict[str, list[str]]]:
    cleaned = response_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()
    value = json.loads(cleaned)
    if not isinstance(value, dict):
        raise ValueError("Qwen retrieval response is not an object")
    output: dict[str, str | None] = {}
    flags: dict[str, list[str]] = {field: [] for field in SECTION_FIELDS}
    for field in SECTION_FIELDS:
        summary = value.get(field)
        passage_id = value.get(f"{field}_passage_id")
        summary = " ".join(summary.split()) if isinstance(summary, str) and summary.strip() else None
        valid = {row["passage_id"]: row["text"] for row in ranked[field]}
        if summary is None:
            output[field] = None
            output[f"{field}_evidence"] = None
            if passage_id is not None:
                flags[field].append("passage_without_summary")
            continue
        output[field] = summary
        if passage_id in valid:
            output[f"{field}_evidence"] = valid[passage_id]
            output[f"{field}_passage_id"] = passage_id
        else:
            output[f"{field}_evidence"] = None
            output[f"{field}_passage_id"] = None
            flags[field].append("invalid_passage_id")
    return output, flags


def run_paper(
    manifest_root: Path,
    paper: dict[str, Any],
    baseline: dict[str, Any],
    *,
    scorer: Any,
    model: str,
    ollama_url: str,
    timeout: int,
    generate_fn: Callable[..., dict[str, Any]] = call_experiment_ollama,
) -> dict[str, Any]:
    source_text, context_type, source_sha256 = source_context(manifest_root, paper)
    if baseline.get("source_sha256") != source_sha256:
        raise ValueError(f"Paper {paper['paper_id']} baseline source hash does not match")
    passages = split_source_passages(source_text)
    prepared = {
        "paper": paper,
        "baseline": baseline,
        "context_type": context_type,
        "source_sha256": source_sha256,
        "passages": passages,
    }
    return run_prepared_paper(
        prepared,
        scorer=scorer,
        model=model,
        ollama_url=ollama_url,
        timeout=timeout,
        generate_fn=generate_fn,
    )


def run_prepared_paper(
    prepared: dict[str, Any],
    *,
    scorer: Any,
    model: str,
    ollama_url: str,
    timeout: int,
    generate_fn: Callable[..., dict[str, Any]] = call_experiment_ollama,
) -> dict[str, Any]:
    paper = prepared["paper"]
    baseline = prepared["baseline"]
    context_type = prepared["context_type"]
    source_sha256 = prepared["source_sha256"]
    passages = prepared["passages"]
    if not passages:
        raise ValueError(f"Paper {paper['paper_id']} produced no passages")
    ranked = rank_passages(passages, scorer=scorer)
    minilm_only = {
        field: {
            "summary": rows[0]["text"] if rows else None,
            "evidence": rows[0]["text"] if rows else None,
            "passage_id": rows[0]["passage_id"] if rows else None,
            "score": rows[0]["score"] if rows else None,
        }
        for field, rows in ranked.items()
    }
    prompt = build_qwen_retrieval_prompt(paper["title"], ranked)
    response = generate_fn(ollama_url, model, prompt, timeout)
    qwen_fields, qwen_flags = parse_qwen_retrieval_output(response["response"], ranked)
    return {
        "status": "succeeded",
        "paper_id": paper["paper_id"],
        "title": paper["title"],
        "context_type": context_type,
        "source_sha256": source_sha256,
        "passage_count": len(passages),
        "original_signals": baseline.get("original_signals"),
        "qwen_baseline": baseline.get("new_fields"),
        "retrieved_passages": ranked,
        "minilm_only": minilm_only,
        "minilm_qwen": qwen_fields,
        "minilm_qwen_flags": qwen_flags,
        "qwen_raw_response": response["response"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run frozen MiniLM passage extraction experiment.")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--baseline-results", type=Path)
    parser.add_argument("--prepared-input", type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="qwen2.5:1.5b-instruct")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434/api/generate")
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()

    if args.prepare_only:
        if not args.manifest or not args.baseline_results or args.prepared_input:
            parser.error("--prepare-only requires --manifest and --baseline-results only")
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        baseline_payload = json.loads(args.baseline_results.read_text(encoding="utf-8"))
        baselines = {
            int(row["paper_id"]): row
            for row in baseline_payload["results"]
            if row.get("status") == "succeeded"
        }
        prepared_rows = []
        for paper in manifest["papers"]:
            baseline = baselines.get(int(paper["paper_id"]))
            if baseline is None:
                continue
            source_text, context_type, source_sha256 = source_context(args.manifest.parent, paper)
            if baseline.get("source_sha256") != source_sha256:
                raise ValueError(f"Paper {paper['paper_id']} baseline source hash does not match")
            prepared_rows.append({
                "paper": paper,
                "baseline": baseline,
                "context_type": context_type,
                "source_sha256": source_sha256,
                "passages": split_source_passages(source_text),
            })
        payload = {
            "experiment_version": EXPERIMENT_VERSION,
            "manifest_sha256": hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
            "prepared_at": datetime.now(timezone.utc).isoformat(),
            "papers": prepared_rows,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        print(json.dumps({"status": "prepared", "paper_count": len(prepared_rows), "output": str(args.output)}))
        return

    if not args.prepared_input or args.manifest or args.baseline_results:
        parser.error("run mode requires --prepared-input without --manifest or --baseline-results")
    prepared_payload = json.loads(args.prepared_input.read_text(encoding="utf-8"))
    prepared_rows = prepared_payload["papers"]
    scorer = MiniLMScorer()
    started_at = datetime.now(timezone.utc).isoformat()
    results = []
    for prepared in prepared_rows:
        paper = prepared["paper"]
        started = time.monotonic()
        try:
            result = run_prepared_paper(
                prepared,
                scorer=scorer,
                model=args.model,
                ollama_url=args.ollama_url,
                timeout=args.timeout,
            )
        except Exception as error:
            result = {
                "status": "failed",
                "paper_id": paper["paper_id"],
                "title": paper["title"],
                "error_type": type(error).__name__,
                "error": str(error),
            }
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        results.append(result)
        payload = {
            "experiment_version": EXPERIMENT_VERSION,
            "manifest_sha256": prepared_payload["manifest_sha256"],
            "run_started_at": started_at,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "results": results,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        temporary.replace(args.output)
    print(json.dumps({"status": "complete", "paper_count": len(results), "output": str(args.output)}))


if __name__ == "__main__":
    main()
