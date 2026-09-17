#!/usr/bin/env python3
"""Create a bounded, locally assessed copy of a frozen ranking corpus."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paper_agents.ranking_quality_assessor import _pdf_paths, assess_fixture
from paper_agents.ranking_quality_replay import paths_collide


def main() -> None:
    parser = argparse.ArgumentParser(description="Assess primary PDFs for offline ranking replay")
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--db", type=Path, default=Path("data/paper_agent.db"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--max-papers", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--model", default="qwen2.5:1.5b-instruct")
    parser.add_argument("--ollama-url", default="http://localhost:11434/api/generate")
    parser.add_argument("--two-column-a4-paper-id", type=int, action="append", default=[])
    parser.add_argument("--primary-pdf", action="append", default=[], metavar="PAPER_ID=PATH")
    parser.add_argument("--conversion-only", action="store_true")
    parser.add_argument("--paper-id", type=int, action="append", default=[])
    args = parser.parse_args()
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    primary_paths = {}
    for assignment in args.primary_pdf:
        paper_id, separator, path = assignment.partition("=")
        if not separator or not paper_id.isdigit() or not path:
            parser.error("--primary-pdf must use PAPER_ID=PATH")
        primary_paths[paper_id] = Path(path)
    ids = [str(item.get("id") or "") for item in fixture.get("candidates", [])]
    all_primary_paths = _pdf_paths(args.db, ids)
    all_primary_paths.update(primary_paths)
    protected = [args.fixture, args.db, *all_primary_paths.values()]
    for destination in (args.output, args.report):
        if any(paths_collide(source, destination) for source in protected):
            parser.error("outputs must not overwrite the fixture, database, or any primary source")
        if destination.exists():
            parser.error("diagnostic outputs must be new files")
    if paths_collide(args.output, args.report):
        parser.error("--output and --report must be different files")
    reserved = []
    try:
        for destination in (args.output, args.report):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.open("x", encoding="utf-8").close()
            reserved.append(destination)
        assessed, report = assess_fixture(
            fixture, args.db, max_papers=args.max_papers, timeout=args.timeout,
            model=args.model, ollama_url=args.ollama_url,
            pdf_layouts={str(paper_id): "two-column-a4" for paper_id in args.two_column_a4_paper_id},
            primary_paths=primary_paths, conversion_only=args.conversion_only,
            target_ids={str(paper_id) for paper_id in args.paper_id} or None,
        )
        args.output.write_text(json.dumps(assessed, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except BaseException:
        for destination in reserved:
            destination.unlink(missing_ok=True)
        raise
    print(json.dumps({"completed": report["completed"], "attempted": report["attempted"],
                      "pdf_artifact_count": report["pdf_artifact_count"],
                      "model_calls": report["model_calls"]}))


if __name__ == "__main__":
    main()
