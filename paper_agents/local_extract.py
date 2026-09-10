from __future__ import annotations

import concurrent.futures
import json
import re
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from paper_agents.runtime_config import ollama_url as configured_ollama_url


DEFAULT_MODEL = "qwen2.5:1.5b-instruct"
DEFAULT_OLLAMA_URL = configured_ollama_url()
DEFAULT_EXTRACTION_DIR = Path("data/extractions")
ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom"}
REQUIRED_KEYS = [
    "paper_date",
    "research_problem",
    "why_it_matters",
    "approach",
]


def extract_paper(
    source_path: Path,
    *,
    model: str = DEFAULT_MODEL,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    max_chars: int = 7000,
    limit_chunks: int = 3,
    timeout: int = 600,
    workers: int = 1,
) -> dict[str, Any]:
    text_path, cleanup_dir = prepare_text_source(source_path)
    try:
        text = text_path.read_text(encoding="utf-8", errors="ignore")
        chunks = chunk_text(text, max_chars)
        if limit_chunks > 0:
            chunks = chunks[:limit_chunks]

        chunk_results = extract_chunks(
            ollama_url,
            model,
            chunks,
            timeout,
            workers=max(1, workers),
        )

        synthesis = synthesize_extractions(ollama_url, model, chunk_results, timeout)
        fallback = deterministic_merge(chunk_results)
        merged = synthesis["extraction"]
        merge_strategy = "synthesis"
        if populated_field_count(merged) < populated_field_count(fallback):
            merged = fallback
            merge_strategy = "deterministic_fallback"

        source_metadata = lookup_source_metadata(source_path)
        if source_metadata.get("paper_date"):
            merged["paper_date"] = source_metadata["paper_date"]

        return {
            "source": str(source_path),
            "text_source": str(text_path),
            "source_metadata": source_metadata,
            "model": model,
            "chunk_count": len(chunk_results),
            "merged": merged,
            "merge_strategy": merge_strategy,
            "synthesis": synthesis,
            "chunks": chunk_results,
        }
    finally:
        if cleanup_dir is not None:
            cleanup_dir.cleanup()


def prepare_text_source(source_path: Path) -> tuple[Path, tempfile.TemporaryDirectory[str] | None]:
    if source_path.suffix.lower() != ".pdf":
        return source_path, None

    cleanup_dir = tempfile.TemporaryDirectory()
    text_path = Path(cleanup_dir.name) / f"{source_path.stem}.txt"
    try:
        subprocess.run(
            ["pdftotext", str(source_path), str(text_path)],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        cleanup_dir.cleanup()
        raise RuntimeError(
            "pdftotext is required for PDF input. Install poppler-utils."
        ) from exc
    except subprocess.CalledProcessError as exc:
        cleanup_dir.cleanup()
        message = exc.stderr.strip() or exc.stdout.strip() or "pdftotext failed"
        raise RuntimeError(message) from exc

    return text_path, cleanup_dir


def chunk_text(text: str, max_chars: int) -> list[str]:
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current = ""

    for paragraph in paragraphs:
        if len(paragraph) > max_chars:
            if current:
                chunks.append(current.strip())
                current = ""
            for start in range(0, len(paragraph), max_chars):
                chunks.append(paragraph[start : start + max_chars].strip())
            continue

        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) > max_chars and current:
            chunks.append(current.strip())
            current = paragraph
        else:
            current = candidate

    if current:
        chunks.append(current.strip())

    return chunks


def build_chunk_prompt(chunk: str) -> str:
    return (
        "You are an information extraction function. Output exactly one JSON "
        f"object matching this schema: {schema_text()}. "
        "Values must be strings or null. Use only the excerpt. Do not include "
        "markdown, extra keys, arrays, findings, or line breaks inside values.\n\n"
        f"Excerpt:\n{chunk}"
    )


def build_synthesis_prompt(chunk_extractions: list[dict[str, str | None]]) -> str:
    return (
        "You are merging chunk-level paper metadata extractions. Output exactly "
        f"one JSON object matching this schema: {schema_text()}. "
        "Values must be strings or null. Use only the chunk extractions. Prefer "
        "paper-level facts over example-specific details. For paper_date, use the "
        "most specific explicitly stated date. Do not include markdown, extra keys, "
        "arrays, findings, or line breaks inside values.\n\n"
        "Chunk extractions:\n"
        f"{json.dumps(chunk_extractions, indent=2, ensure_ascii=False)}"
    )


def schema_text() -> str:
    schema = {
        "paper_date": "string|null",
        "research_problem": "string|null",
        "why_it_matters": "string|null",
        "approach": "string|null",
    }
    return json.dumps(schema, separators=(",", ":"))


def call_ollama(url: str, model: str, prompt: str, timeout: int) -> dict[str, Any]:
    payload = {
        "model": model,
        "prompt": prompt,
        "format": "json",
        "stream": False,
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def normalize_extraction(value: Any) -> dict[str, str | None]:
    if not isinstance(value, dict):
        raise ValueError("model response is not a JSON object")

    normalized: dict[str, str | None] = {}
    for key in REQUIRED_KEYS:
        item = value.get(key)
        if item is None:
            normalized[key] = None
        elif isinstance(item, str):
            normalized[key] = " ".join(item.split()) or None
        else:
            normalized[key] = json.dumps(item, ensure_ascii=False)
    return normalized


def parse_model_json(response_text: str) -> dict[str, str | None]:
    cleaned = response_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()
    parsed = json.loads(cleaned)
    return normalize_extraction(parsed)


def extract_chunks(
    url: str,
    model: str,
    chunks: list[str],
    timeout: int,
    *,
    workers: int,
) -> list[dict[str, Any]]:
    if workers == 1 or len(chunks) <= 1:
        results = []
        for index, chunk in enumerate(chunks):
            print(f"extracting chunk {index + 1}/{len(chunks)} ({len(chunk)} chars)")
            results.append(extract_chunk(url, model, chunk, index, timeout))
        return results

    results: list[dict[str, Any] | None] = [None] * len(chunks)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {}
        for index, chunk in enumerate(chunks):
            print(f"queueing chunk {index + 1}/{len(chunks)} ({len(chunk)} chars)")
            future = executor.submit(extract_chunk, url, model, chunk, index, timeout)
            futures[future] = index

        for future in concurrent.futures.as_completed(futures):
            index = futures[future]
            results[index] = future.result()
            print(f"finished chunk {index + 1}/{len(chunks)}")

    return [result for result in results if result is not None]


def extract_chunk(
    url: str,
    model: str,
    chunk: str,
    index: int,
    timeout: int,
) -> dict[str, Any]:
    return run_extraction_call(
        url,
        model,
        build_chunk_prompt(chunk),
        timeout,
        {"chunk_index": index, "chars": len(chunk)},
    )


def synthesize_extractions(
    url: str,
    model: str,
    chunks: list[dict[str, Any]],
    timeout: int,
) -> dict[str, Any]:
    extractions = [
        chunk["extraction"]
        for chunk in chunks
        if populated_field_count(chunk["extraction"]) > 0
    ]
    return run_extraction_call(
        url,
        model,
        build_synthesis_prompt(extractions),
        timeout,
        {"chunk_index": None, "chars": 0, "kind": "synthesis"},
    )


def deterministic_merge(chunks: list[dict[str, Any]]) -> dict[str, str | None]:
    merged: dict[str, str | None] = {key: None for key in REQUIRED_KEYS}
    for key in REQUIRED_KEYS:
        for chunk in chunks:
            value = chunk["extraction"].get(key)
            if value:
                merged[key] = value
                break
    return merged


def populated_field_count(extraction: dict[str, str | None]) -> int:
    return sum(1 for key in REQUIRED_KEYS if extraction.get(key))


def run_extraction_call(
    url: str,
    model: str,
    prompt: str,
    timeout: int,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    started = time.time()
    raw = call_ollama(url, model, prompt, timeout)
    elapsed = time.time() - started
    response_text = raw.get("response", "")

    try:
        extraction = parse_model_json(response_text)
        error = None
    except (json.JSONDecodeError, ValueError) as exc:
        extraction = {key: None for key in REQUIRED_KEYS}
        error = str(exc)

    return {
        **metadata,
        "wall_clock_sec": round(elapsed, 2),
        "ollama_total_sec": round(raw.get("total_duration", 0) / 1e9, 2),
        "prompt_eval_count": raw.get("prompt_eval_count"),
        "eval_count": raw.get("eval_count"),
        "error": error,
        "extraction": extraction,
        "raw_response": response_text,
    }


def lookup_source_metadata(source_path: Path) -> dict[str, Any]:
    arxiv_id = arxiv_id_from_source(source_path)
    if not arxiv_id:
        return {}

    metadata: dict[str, Any] = {"source": "arxiv", "arxiv_id": arxiv_id}
    try:
        metadata.update(fetch_arxiv_metadata(arxiv_id))
    except OSError as exc:
        metadata["metadata_error"] = str(exc)
    return metadata


def fetch_arxiv_metadata(arxiv_id: str, timeout: int = 30) -> dict[str, Any]:
    clean_id = arxiv_id.replace("_", "/")
    params = urllib.parse.urlencode({"id_list": clean_id})
    request = urllib.request.Request(
        f"https://export.arxiv.org/api/query?{params}",
        headers={"User-Agent": "paper-agent/0.1"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        root = ET.fromstring(response.read())

    entry = root.find("atom:entry", ARXIV_NS)
    if entry is None:
        return {}

    return {
        "paper_date": entry.findtext("atom:published", default="", namespaces=ARXIV_NS)[:10] or None,
        "updated": entry.findtext("atom:updated", default="", namespaces=ARXIV_NS)[:10] or None,
        "title": " ".join(entry.findtext("atom:title", default="", namespaces=ARXIV_NS).split()) or None,
        "url": arxiv_abs_url(clean_id),
    }


def arxiv_id_from_source(source_path: Path) -> str | None:
    parts = source_path.parts
    if "arxiv" in parts:
        candidate = source_path.stem
        if is_arxiv_id(candidate):
            return candidate

    text = str(source_path)
    match = re.search(r"(?:arxiv(?:\.org)?/(?:abs|pdf)/|arxiv[_-])([0-9]{4}\.[0-9]{4,5}(?:v[0-9]+)?)", text)
    if match:
        return match.group(1)

    stem_match = re.search(r"([0-9]{4}\.[0-9]{4,5}(?:v[0-9]+)?)", source_path.stem)
    if stem_match:
        return stem_match.group(1)
    return None


def is_arxiv_id(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9]{4}\.[0-9]{4,5}(?:v[0-9]+)?", value))


def arxiv_abs_url(arxiv_id: str) -> str:
    return f"https://arxiv.org/abs/{arxiv_id}"


def output_path_for(source_path: Path, model: str) -> Path:
    model_slug = model.replace(":", "-").replace("/", "-")
    arxiv_id = arxiv_id_from_source(source_path)
    if arxiv_id:
        return DEFAULT_EXTRACTION_DIR / "arxiv" / f"{arxiv_id}.{model_slug}.summary.json"

    source_slug = safe_output_slug(source_path.stem)
    return DEFAULT_EXTRACTION_DIR / "manual" / f"{source_slug}.{model_slug}.summary.json"


def safe_output_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._")
    return slug[:120] if slug else "paper"
