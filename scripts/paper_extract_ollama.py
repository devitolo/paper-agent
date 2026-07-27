#!/usr/bin/env python3
"""Extract structured paper notes from text using a local Ollama model.

Example:

    pdftotext paper.pdf paper.txt
    python3 scripts/paper_extract_ollama.py paper.txt --model qwen2.5:1.5b-instruct

The script writes a JSON file containing per-chunk extractions plus a simple
merged view. It is intended as a benchmark/prototype path before this behavior
is wired into the main `paper_agents` package.
"""

from __future__ import annotations

import argparse
import json
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


def build_prompt(chunk: str) -> str:
    schema = {
        "problem": "string|null",
        "why_hard": "string|null",
        "proposed_use_of_llms": "string|null",
        "dataset_or_scale": "string|null",
        "evaluation_method": "string|null",
    }
    return (
        "You are an information extraction function. Output exactly one JSON "
        "object matching this schema: "
        f"{json.dumps(schema, separators=(',', ':'))}. "
        "Values must be strings or null. Use only the excerpt. Do not include "
        "markdown, extra keys, arrays, findings, or line breaks inside values.\n\n"
        f"Excerpt:\n{chunk}"
    )


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
    started = time.time()
    raw = call_ollama(url, model, build_prompt(chunk), timeout)
    elapsed = time.time() - started
    response_text = raw.get("response", "")

    try:
        extraction = parse_model_json(response_text)
        error = None
    except (json.JSONDecodeError, ValueError) as exc:
        extraction = {key: None for key in REQUIRED_KEYS}
        error = str(exc)

    return {
        "chunk_index": index,
        "chars": len(chunk),
        "wall_clock_sec": round(elapsed, 2),
        "ollama_total_sec": round(raw.get("total_duration", 0) / 1e9, 2),
        "prompt_eval_count": raw.get("prompt_eval_count"),
        "eval_count": raw.get("eval_count"),
        "error": error,
        "extraction": extraction,
        "raw_response": response_text,
    }


def merge_extractions(chunks: list[dict[str, Any]]) -> dict[str, str | None]:
    merged: dict[str, str | None] = {key: None for key in REQUIRED_KEYS}
    for key in REQUIRED_KEYS:
        for chunk in chunks:
            value = chunk["extraction"].get(key)
            if value:
                merged[key] = value
                break
    return merged


def output_path_for(text_path: Path, model: str) -> Path:
    model_slug = model.replace(":", "-").replace("/", "-")
    return text_path.with_suffix(f".{model_slug}.summary.json")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("text_file", help="Text file extracted from a paper PDF")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    parser.add_argument("--max-chars", type=int, default=7000)
    parser.add_argument("--limit-chunks", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--output", help="Output JSON path")
    args = parser.parse_args()

    text_path = Path(args.text_file)
    text = text_path.read_text(encoding="utf-8", errors="ignore")
    chunks = chunk_text(text, args.max_chars)
    if args.limit_chunks > 0:
        chunks = chunks[: args.limit_chunks]

    results = []
    for index, chunk in enumerate(chunks):
        print(f"extracting chunk {index + 1}/{len(chunks)} ({len(chunk)} chars)")
        results.append(
            extract_chunk(args.ollama_url, args.model, chunk, index, args.timeout)
        )

    output = {
        "source": str(text_path),
        "model": args.model,
        "chunk_count": len(results),
        "merged": merge_extractions(results),
        "chunks": results,
    }

    output_path = Path(args.output) if args.output else output_path_for(text_path, args.model)
    output_path.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {output_path}")
    print(json.dumps(output["merged"], indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
