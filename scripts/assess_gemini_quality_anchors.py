#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from paper_agents.gemini_quality_experiment import assess
from paper_agents.ranking_quality_replay import paths_collide


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline Gemini contribution-quality anchor test")
    parser.add_argument("corpus", type=Path)
    parser.add_argument("--paper-id", action="append", required=True)
    parser.add_argument("--model")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if paths_collide(args.corpus, args.output) or args.output.exists():
        parser.error("--output must be a new file distinct from the corpus")
    report = assess(json.loads(args.corpus.read_text()), paper_ids=set(args.paper_id),
                    model=args.model, timeout=args.timeout)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "calls": report["calls"],
                      "output": str(args.output)}))


if __name__ == "__main__":
    main()
