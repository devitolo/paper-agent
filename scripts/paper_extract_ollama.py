#!/usr/bin/env python3
"""Extract structured paper notes from text or PDF using a local Ollama model.

Example:

    python3 scripts/paper_extract_ollama.py paper.pdf --model qwen2.5:1.5b-instruct

The script writes a JSON file containing per-chunk extractions plus a synthesized
merged view. It is intended as a benchmark/prototype path before this behavior
is wired into the main workflow.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paper_agents.local_extract import (
    DEFAULT_MODEL,
    DEFAULT_OLLAMA_URL,
    extract_paper,
    output_path_for,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", help="PDF or text file to extract")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    parser.add_argument("--max-chars", type=int, default=7000)
    parser.add_argument("--limit-chunks", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--output", help="Output JSON path")
    args = parser.parse_args()

    source_path = Path(args.source)
    output = extract_paper(
        source_path,
        model=args.model,
        ollama_url=args.ollama_url,
        max_chars=args.max_chars,
        limit_chunks=args.limit_chunks,
        timeout=args.timeout,
        workers=args.workers,
    )

    output_path = Path(args.output) if args.output else output_path_for(source_path, args.model)
    output_path.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {output_path}")
    print(json.dumps(output["merged"], indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
