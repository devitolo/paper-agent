from __future__ import annotations

import json
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_MODEL = "qwen2.5:1.5b-instruct"
DEFAULT_OLLAMA_URL = "http://localhost:11434/api/generate"
REQUIRED_KEYS = [
    "problem",
    "why_hard",
    "proposed_use_of_llms",
    "dataset_or_scale",
    "evaluation_method",
]


def extract_paper(
    source_path: Path,
    *,
    model: str = DEFAULT_MODEL,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    max_chars: int = 7000,
    limit_chunks: int = 3,
    timeout: int = 600,
) -> dict[str, Any]:
    text_path, cleanup_dir = prepare_text_source(source_path)
    try:
        text = text_path.read_text(encoding="utf-8", errors="ignore")
        chunks = chunk_text(text, max_chars)
        if limit_chunks > 0:
            chunks = chunks[:limit_chunks]

        chunk_results = []
        for index, chunk in enumerate(chunks):
            print(f"extracting chunk {index + 1}/{len(chunks)} ({len(chunk)} chars)")
            chunk_results.append(
                extract_chunk(ollama_url, model, chunk, index, timeout)
            )

        merged = synthesize_extractions(ollama_url, model, chunk_results, timeout)
        return {
            "source": str(source_path),
            "text_source": str(text_path),
            "model": model,
            "chunk_count": len(chunk_results),
            "merged": merged["extraction"],
            "synthesis": merged,
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
        "paper-level facts over example-specific incident details. Keep dataset "
        "or scale facts in dataset_or_scale, not evaluation_method. Do not include "
        "markdown, extra keys, arrays, findings, or line breaks inside values.\n\n"
        "Chunk extractions:\n"
        f"{json.dumps(chunk_extractions, indent=2, ensure_ascii=False)}"
    )


def schema_text() -> str:
    schema = {
        "problem": "string|null",
        "why_hard": "string|null",
        "proposed_use_of_llms": "string|null",
        "dataset_or_scale": "string|null",
        "evaluation_method": "string|null",
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
            normalized[key] = " ".join(item.split())
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
    extractions = [chunk["extraction"] for chunk in chunks]
    return run_extraction_call(
        url,
        model,
        build_synthesis_prompt(extractions),
        timeout,
        {"chunk_index": None, "chars": 0, "kind": "synthesis"},
    )


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


def output_path_for(source_path: Path, model: str) -> Path:
    model_slug = model.replace(":", "-").replace("/", "-")
    return source_path.with_suffix(f".{model_slug}.summary.json")
