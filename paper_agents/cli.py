from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from paper_agents.curator import ResearchCurator
from paper_agents.feedback import FeedbackAgent
from paper_agents.local_extract import (
    DEFAULT_MODEL,
    DEFAULT_OLLAMA_URL,
    extract_paper,
    output_path_for,
)
from paper_agents.pipeline import (
    DEFAULT_PIPELINE_LIMIT_CHUNKS,
    DEFAULT_PIPELINE_MAX_CHARS,
    DEFAULT_PIPELINE_TIMEOUT,
    DEFAULT_PIPELINE_WORKERS,
    run_daily_pipeline,
)
from paper_agents.scout import (
    DEFAULT_FETCH_LIMIT,
    DEFAULT_FRESHNESS_MONTHS,
    DEFAULT_KEEP_LIMIT,
    DEFAULT_PDF_DIR,
    DEFAULT_SCOUT_DIR,
    DEFAULT_SCOUT_TOPICS,
    ResearchScout,
    run_daily_scout,
)
from paper_agents.store import DEFAULT_PROFILE_PATH, load_profile, save_profile


ENV_PATH = Path(".env")


def main() -> None:
    load_dotenv(ENV_PATH)

    parser = argparse.ArgumentParser(description="Personal research-paper agents prototype")
    parser.add_argument(
        "--profile",
        type=Path,
        default=DEFAULT_PROFILE_PATH,
        help="Path to the JSON preference profile",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run Scout, then Curator")
    run_parser.add_argument("--max-results", type=int, default=10, help="Number of arXiv results to fetch")

    daily_parser = subparsers.add_parser("scout-daily", help="Run deterministic daily arXiv scout MVP")
    daily_parser.add_argument("--freshness-months", type=int, default=DEFAULT_FRESHNESS_MONTHS)
    daily_parser.add_argument("--fetch", type=int, default=DEFAULT_FETCH_LIMIT, help="Maximum candidates to fetch")
    daily_parser.add_argument("--keep", type=int, default=DEFAULT_KEEP_LIMIT, help="Number of top candidates to select")
    daily_parser.add_argument("--scout-dir", type=Path, default=DEFAULT_SCOUT_DIR)
    daily_parser.add_argument("--pdf-dir", type=Path, default=DEFAULT_PDF_DIR)
    daily_parser.add_argument("--topic", action="append", dest="topics", help="Topic to search; repeatable")
    daily_parser.add_argument("--no-download", action="store_true", help="Do not download PDFs")

    pipeline_parser = subparsers.add_parser("pipeline-daily", help="Run Scout, download PDFs, and extract triage cards")
    pipeline_parser.add_argument("--freshness-months", type=int, default=DEFAULT_FRESHNESS_MONTHS)
    pipeline_parser.add_argument("--fetch", type=int, default=DEFAULT_FETCH_LIMIT, help="Maximum candidates to fetch")
    pipeline_parser.add_argument("--keep", type=int, default=DEFAULT_KEEP_LIMIT, help="Number of top candidates to select and extract")
    pipeline_parser.add_argument("--scout-dir", type=Path, default=DEFAULT_SCOUT_DIR)
    pipeline_parser.add_argument("--pdf-dir", type=Path, default=DEFAULT_PDF_DIR)
    pipeline_parser.add_argument("--topic", action="append", dest="topics", help="Topic to search; repeatable")
    pipeline_parser.add_argument("--model", default=DEFAULT_MODEL)
    pipeline_parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    pipeline_parser.add_argument("--max-chars", type=int, default=DEFAULT_PIPELINE_MAX_CHARS)
    pipeline_parser.add_argument("--limit-chunks", type=int, default=None)
    pipeline_parser.add_argument("--quick", action="store_true", help="Interactive preview mode; extract only the first 2 chunks per paper unless --limit-chunks is set")
    pipeline_parser.add_argument("--timeout", type=int, default=DEFAULT_PIPELINE_TIMEOUT)
    pipeline_parser.add_argument("--workers", type=int, default=DEFAULT_PIPELINE_WORKERS)

    feedback_parser = subparsers.add_parser("feedback", help="Update preferences from feedback")
    feedback_parser.add_argument("text", help="Natural-language feedback")

    extract_parser = subparsers.add_parser("extract", help="Extract paper metadata with local Ollama")
    extract_parser.add_argument("source", type=Path, help="PDF or text file to extract")
    extract_parser.add_argument("--model", default=DEFAULT_MODEL)
    extract_parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    extract_parser.add_argument("--max-chars", type=int, default=7000)
    extract_parser.add_argument("--limit-chunks", type=int, default=3)
    extract_parser.add_argument("--timeout", type=int, default=600)
    extract_parser.add_argument("--workers", type=int, default=1)
    extract_parser.add_argument("--output", type=Path, help="Output JSON path")

    subparsers.add_parser("profile", help="Print the current preference profile")

    args = parser.parse_args()

    if args.command == "run":
        require_openai_api_key()
        profile = load_profile(args.profile)
        scout = ResearchScout(max_results=args.max_results)
        curator = ResearchCurator()

        try:
            candidates = scout.run(profile)
            recommendations = curator.run(profile, candidates)
        except RuntimeError as error:
            raise SystemExit(str(error)) from error

        print_section("Scout candidates", {"candidates": candidates})
        print_section("Curator recommendations", {"recommendations": recommendations})
        return

    if args.command == "scout-daily":
        topics = args.topics or DEFAULT_SCOUT_TOPICS
        try:
            output = run_daily_scout(
                topics=topics,
                freshness_months=args.freshness_months,
                fetch_limit=args.fetch,
                keep_limit=args.keep,
                scout_dir=args.scout_dir,
                pdf_dir=args.pdf_dir,
                download_pdfs=not args.no_download,
            )
        except RuntimeError as error:
            raise SystemExit(str(error)) from error
        print_section("Daily scout", output)
        return

    if args.command == "pipeline-daily":
        limit_chunks = resolve_pipeline_limit_chunks(args.quick, args.limit_chunks)
        mode = "quick" if args.quick else "full"
        print(f"pipeline mode: {mode} (limit_chunks={limit_chunks})")
        try:
            output = run_daily_pipeline(
                topics=args.topics or DEFAULT_SCOUT_TOPICS,
                freshness_months=args.freshness_months,
                fetch_limit=args.fetch,
                keep_limit=args.keep,
                scout_dir=args.scout_dir,
                pdf_dir=args.pdf_dir,
                model=args.model,
                ollama_url=args.ollama_url,
                max_chars=args.max_chars,
                limit_chunks=limit_chunks,
                timeout=args.timeout,
                workers=args.workers,
            )
        except RuntimeError as error:
            raise SystemExit(str(error)) from error
        print_section("Pipeline review cards", {"cards": output["cards"]})
        return

    if args.command == "feedback":
        require_openai_api_key()
        profile = load_profile(args.profile)
        try:
            updated_profile = FeedbackAgent().run(profile, args.text)
        except RuntimeError as error:
            raise SystemExit(str(error)) from error
        save_profile(updated_profile, args.profile)
        print_section("Updated profile", updated_profile)
        return

    if args.command == "extract":
        try:
            output = extract_paper(
                args.source,
                model=args.model,
                ollama_url=args.ollama_url,
                max_chars=args.max_chars,
                limit_chunks=args.limit_chunks,
                timeout=args.timeout,
                workers=args.workers,
            )
        except RuntimeError as error:
            raise SystemExit(str(error)) from error

        output_path = args.output if args.output else output_path_for(args.source, args.model)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n")
        print(f"wrote {output_path}")
        print_section("Local extraction", output["merged"])
        return

    if args.command == "profile":
        print(json.dumps(load_profile(args.profile), indent=2, ensure_ascii=False))


def resolve_pipeline_limit_chunks(quick: bool, limit_chunks: int | None) -> int:
    if limit_chunks is not None:
        return limit_chunks
    if quick:
        return 2
    return DEFAULT_PIPELINE_LIMIT_CHUNKS

def print_section(title: str, payload: dict) -> None:
    print(f"\n## {title}")
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def require_openai_api_key() -> None:
    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit(
            "OPENAI_API_KEY is not set. Add it to .env or export it before running this command."
        )


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


if __name__ == "__main__":
    main()
