from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from paper_agents.bootstrap import bootstrap_known_papers
from paper_agents.curator import ResearchCurator
from paper_agents.db import (
    DEFAULT_DB_PATH,
    DEFAULT_SCHEMA_PATH,
    db_stats,
    init_db,
    list_papers,
    recent_runs,
    reset_db,
    seen_source_ids,
)
from paper_agents.feedback import FeedbackAgent
from paper_agents.local_extract import (
    DEFAULT_MODEL,
    DEFAULT_OLLAMA_URL,
    extract_paper,
    output_path_for,
)
from paper_agents.pipeline import (
    DEFAULT_MAX_SCOUT_ATTEMPTS,
    DEFAULT_PIPELINE_LIMIT_CHUNKS,
    DEFAULT_PIPELINE_MAX_CHARS,
    DEFAULT_PIPELINE_TIMEOUT,
    DEFAULT_PIPELINE_WORKERS,
    run_daily_pipeline,
)
from paper_agents.review import (
    DEFAULT_REVIEW_DIR,
    DEFAULT_REVIEW_MAX_CHARS,
    review_summary,
)
from paper_agents.scout import (
    DEFAULT_ARXIV_REQUEST_DELAY,
    DEFAULT_ARXIV_RETRIES,
    DEFAULT_ARXIV_TIMEOUT,
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
from paper_agents.web import run_review_ui


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

    db_parser = subparsers.add_parser("db", help="Database utilities")
    db_subparsers = db_parser.add_subparsers(dest="db_command", required=True)
    db_init_parser = db_subparsers.add_parser("init", help="Initialize the local SQLite database")
    db_init_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="SQLite database path")
    db_init_parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA_PATH, help="Schema SQL path")
    db_reset_parser = db_subparsers.add_parser("reset", help="Delete and recreate the local SQLite database")
    db_reset_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="SQLite database path")
    db_reset_parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA_PATH, help="Schema SQL path")
    db_reset_parser.add_argument("--yes", action="store_true", help="Confirm destructive database reset")
    db_stats_parser = db_subparsers.add_parser("stats", help="Show SQLite registry counts")
    db_stats_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="SQLite database path")
    db_runs_parser = db_subparsers.add_parser("recent-runs", help="Show recent Scout/pipeline runs")
    db_runs_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="SQLite database path")
    db_runs_parser.add_argument("--limit", type=int, default=10, help="Number of runs to show")
    db_papers_parser = db_subparsers.add_parser("papers", help="Show recent papers in the registry")
    db_papers_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="SQLite database path")
    db_papers_parser.add_argument("--limit", type=int, default=20, help="Number of papers to show")
    db_papers_parser.add_argument(
        "--selected",
        action="store_true",
        help="Only show papers selected by the latest scout ranking",
    )

    bootstrap_parser = subparsers.add_parser("bootstrap", help="Backfill known Project Paper seed data")
    bootstrap_subparsers = bootstrap_parser.add_subparsers(dest="bootstrap_command", required=True)
    known_parser = bootstrap_subparsers.add_parser("known-papers", help="Insert known seed papers into the V2 registry")
    known_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="SQLite database path")

    run_parser = subparsers.add_parser("run", help="Run Scout, then Curator")
    run_parser.add_argument("--max-results", type=int, default=10, help="Number of arXiv results to fetch")

    daily_parser = subparsers.add_parser("scout-daily", help="Run deterministic daily arXiv scout MVP")
    daily_parser.add_argument("--freshness-months", type=int, default=DEFAULT_FRESHNESS_MONTHS)
    daily_parser.add_argument("--fetch", type=int, default=DEFAULT_FETCH_LIMIT, help="Maximum candidates to fetch")
    daily_parser.add_argument("--keep", type=int, default=DEFAULT_KEEP_LIMIT, help="Number of top candidates to select")
    daily_parser.add_argument("--scout-dir", type=Path, default=DEFAULT_SCOUT_DIR)
    daily_parser.add_argument("--pdf-dir", type=Path, default=DEFAULT_PDF_DIR)
    daily_parser.add_argument("--topic", action="append", dest="topics", help="Topic to search; repeatable")
    daily_parser.add_argument(
        "--request-delay",
        type=float,
        default=DEFAULT_ARXIV_REQUEST_DELAY,
        help="Seconds to wait between arXiv topic requests",
    )
    daily_parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_ARXIV_RETRIES,
        help="Retries per arXiv topic request",
    )
    daily_parser.add_argument(
        "--source-timeout",
        type=int,
        default=DEFAULT_ARXIV_TIMEOUT,
        help="Timeout seconds per arXiv request",
    )
    daily_parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help="SQLite database path for history filtering",
    )
    daily_parser.add_argument(
        "--include-seen",
        action="store_true",
        help="Allow papers already present in SQLite to be selected again",
    )
    daily_parser.add_argument("--no-download", action="store_true", help="Do not download PDFs")

    pipeline_parser = subparsers.add_parser("pipeline-daily", help="Run Scout, download PDFs, and extract triage cards")
    pipeline_parser.add_argument("--freshness-months", type=int, default=DEFAULT_FRESHNESS_MONTHS)
    pipeline_parser.add_argument("--fetch", type=int, default=DEFAULT_FETCH_LIMIT, help="Maximum candidates to fetch")
    pipeline_parser.add_argument("--keep", type=int, default=3, help="Maximum recommendations to curate and extract; capped at 3")
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
    pipeline_parser.add_argument(
        "--request-delay",
        type=float,
        default=DEFAULT_ARXIV_REQUEST_DELAY,
        help="Seconds to wait between arXiv topic requests",
    )
    pipeline_parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_ARXIV_RETRIES,
        help="Retries per arXiv topic request",
    )
    pipeline_parser.add_argument(
        "--source-timeout",
        type=int,
        default=DEFAULT_ARXIV_TIMEOUT,
        help="Timeout seconds per arXiv request",
    )
    pipeline_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="SQLite database path")
    pipeline_parser.add_argument("--max-scout-attempts", type=int, default=DEFAULT_MAX_SCOUT_ATTEMPTS)
    pipeline_parser.add_argument("--min-quality-score", type=float, default=25.0)
    pipeline_parser.add_argument(
        "--include-seen",
        action="store_true",
        help="Deprecated in V2; previously discovered papers are recorded as excluded Scout candidates",
    )

    review_parser = subparsers.add_parser("review-summary", help="Create a ChatGPT section-by-section review from a triage summary JSON")
    review_parser.add_argument("summary", type=Path, help="Local extraction summary JSON")
    review_parser.add_argument("--output-dir", type=Path, default=DEFAULT_REVIEW_DIR)
    review_parser.add_argument("--max-chars", type=int, default=DEFAULT_REVIEW_MAX_CHARS)
    review_parser.add_argument("--timeout", type=int, default=240)

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

    web_parser = subparsers.add_parser("web", help="Run the local review queue web UI")
    web_parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind")
    web_parser.add_argument("--port", type=int, default=8000, help="Port to bind")
    web_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="SQLite database path")

    subparsers.add_parser("profile", help="Print the current preference profile")

    args = parser.parse_args()

    if args.command == "db":
        if args.db_command == "init":
            try:
                output = init_db(args.db, args.schema)
            except RuntimeError as error:
                raise SystemExit(str(error)) from error
            print_section("Database initialized", output)
            return
        if args.db_command == "reset":
            if not args.yes:
                raise SystemExit("Refusing to reset database without --yes")
            print_section("Database reset", reset_db(args.db, args.schema))
            return
        if args.db_command == "stats":
            print_section("Database stats", db_stats(args.db))
            return
        if args.db_command == "recent-runs":
            print_section("Recent database runs", recent_runs(args.db, limit=args.limit))
            return
        if args.db_command == "papers":
            print_section(
                "Database papers",
                list_papers(args.db, limit=args.limit, selected_only=args.selected),
            )
            return

    if args.command == "bootstrap":
        if args.bootstrap_command == "known-papers":
            print_section("Bootstrap known papers", bootstrap_known_papers(db_path=args.db, profile_path=args.profile))
            return

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
        source_seen_ids = seen_source_ids(args.db, source="arxiv") if not args.include_seen else set()
        try:
            output = run_daily_scout(
                topics=topics,
                freshness_months=args.freshness_months,
                fetch_limit=args.fetch,
                keep_limit=args.keep,
                scout_dir=args.scout_dir,
                pdf_dir=args.pdf_dir,
                download_pdfs=not args.no_download,
                request_delay=args.request_delay,
                retries=args.retries,
                timeout=args.source_timeout,
                seen_source_ids=source_seen_ids,
                include_seen=args.include_seen,
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
                db_path=args.db,
                mode=mode,
                request_delay=args.request_delay,
                scout_retries=args.retries,
                scout_timeout=args.source_timeout,
                include_seen=args.include_seen,
                max_scout_attempts=args.max_scout_attempts,
                min_quality_score=args.min_quality_score,
                profile_path=args.profile,
            )
        except RuntimeError as error:
            raise SystemExit(str(error)) from error
        print_section("Pipeline review cards", {"cards": output["cards"]})
        return

    if args.command == "review-summary":
        require_openai_api_key()
        try:
            output = review_summary(
                args.summary,
                output_dir=args.output_dir,
                max_chars=args.max_chars,
                timeout=args.timeout,
            )
        except RuntimeError as error:
            raise SystemExit(str(error)) from error
        print_section("Paper review", output)
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

    if args.command == "web":
        run_review_ui(host=args.host, port=args.port, db_path=args.db)
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
