#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paper_agents.local_extract import DEFAULT_MODEL, DEFAULT_OLLAMA_URL
from paper_agents.metadata_relevance import run_experiment
from paper_agents.ranking_quality_replay import paths_collide


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare keyword and local-Qwen metadata relevance")
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    parser.add_argument("--timeout", type=int, default=45)
    args = parser.parse_args()
    if paths_collide(args.fixture, args.output) or args.output.exists():
        parser.error("--output must be a new file distinct from the fixture")
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with args.output.open("x", encoding="utf-8"):
            pass
    except FileExistsError:
        parser.error("--output must be a new file")

    def checkpoint(value):
        handle, name = tempfile.mkstemp(prefix=f".{args.output.name}.", suffix=".tmp",
                                        dir=args.output.parent)
        temporary = Path(name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, args.output)
        finally:
            temporary.unlink(missing_ok=True)

    report = run_experiment(fixture, model=args.model, url=args.ollama_url,
                            timeout=args.timeout, checkpoint=checkpoint)
    checkpoint(report)
    print(json.dumps({"status": report["status"], "papers": len(report["results"]),
                      "calls": report["calls"], "output": str(args.output)}))


if __name__ == "__main__":
    main()
