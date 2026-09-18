#!/usr/bin/env python3
"""Replay development review labels through the proposed two-axis aggregation."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from paper_agents.ranking_quality_replay import paths_collide
from paper_agents.two_axis_ranking import replay_two_axis


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay development-only two-axis ranking labels")
    parser.add_argument("corpus", type=Path)
    parser.add_argument("labels", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()
    if any(paths_collide(source, args.output) for source in (args.corpus, args.labels)):
        parser.error("--output must not overwrite an input")
    if args.output.exists():
        parser.error("--output must be a new file")
    report = replay_two_axis(
        json.loads(args.corpus.read_text(encoding="utf-8")),
        json.loads(args.labels.read_text(encoding="utf-8")),
        top_k=args.top_k,
    )
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "output": str(args.output)}))


if __name__ == "__main__":
    main()
