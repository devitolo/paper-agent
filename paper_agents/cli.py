from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from paper_agents.bootstrap import bootstrap_known_papers, import_legacy_scout_files
from paper_agents.curator import ResearchCurator
from paper_agents.curator_rescore import rescore_recommendations
from paper_agents.db import (
    DEFAULT_DB_PATH,
    DEFAULT_SCHEMA_PATH,
    cleanup_legacy_feedback_statuses,
    connect_db,
    db_stats,
    health_summary,
    init_db,
    list_papers,
    recent_runs,
    reset_db,
    seen_source_ids,
)
from paper_agents.feedback import FeedbackAgent, apply_feedback_to_profile, ingest_feedback_blob, rebuild_feedback_profile
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
from paper_agents.reviewer_agent import (
    ReviewerConfig,
    backfill_missing_triage_summaries,
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
    ResearchScout,
    SCOUT_SOURCES,
    create_scout_source,
    run_daily_scout,
)
from paper_agents.scout_guidance import guidance_summary, load_scout_guidance, topics_with_guidance
from paper_agents.store import DEFAULT_PROFILE_PATH, load_profile, save_profile
from paper_agents.topics import select_topics_for_source
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
    db_health_parser = db_subparsers.add_parser("health", help="Show workflow and source health")
    db_health_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="SQLite database path")
    db_health_parser.add_argument("--days", type=int, default=21, help="Number of recent days to summarize")
    db_health_parser.add_argument("--source", default="all", help="Optional source filter, e.g. arxiv/openalex")
    db_health_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    db_cleanup_parser = db_subparsers.add_parser(
        "cleanup-legacy-feedback",
        help="Remove obsolete lightweight review statuses from the legacy feedback table",
    )
    db_cleanup_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="SQLite database path")
    db_cleanup_parser.add_argument(
        "--yes",
        action="store_true",
        help="Actually delete matched rows; default is dry-run",
    )

    bootstrap_parser = subparsers.add_parser("bootstrap", help="Backfill known Project Paper seed data")
    bootstrap_subparsers = bootstrap_parser.add_subparsers(dest="bootstrap_command", required=True)
    known_parser = bootstrap_subparsers.add_parser("known-papers", help="Insert known seed papers into the V2 registry")
    known_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="SQLite database path")
    import_scout_parser = bootstrap_subparsers.add_parser("legacy-scout", help="Import old Scout JSONL files into the V2 registry")
    import_scout_parser.add_argument("paths", type=Path, nargs="+", help="Legacy data/scout/*.jsonl files")
    import_scout_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="SQLite database path")
    import_scout_parser.add_argument(
        "--recommend-all-if-unselected",
        action="store_true",
        help="If a JSONL has no selected=true rows, recommend the first three rows",
    )

    run_parser = subparsers.add_parser("run", help="Run Scout, then Curator")
    run_parser.add_argument("--max-results", type=int, default=10, help="Number of arXiv results to fetch")

    daily_parser = subparsers.add_parser("scout-daily", help="Run deterministic daily paper Scout MVP")
    daily_parser.add_argument("--freshness-months", type=int, default=DEFAULT_FRESHNESS_MONTHS)
    daily_parser.add_argument("--fetch", type=int, default=DEFAULT_FETCH_LIMIT, help="Maximum candidates to fetch")
    daily_parser.add_argument("--keep", type=int, default=DEFAULT_KEEP_LIMIT, help="Number of top candidates to select")
    daily_parser.add_argument("--scout-dir", type=Path, default=DEFAULT_SCOUT_DIR)
    daily_parser.add_argument("--pdf-dir", type=Path, default=DEFAULT_PDF_DIR)
    daily_parser.add_argument("--source", choices=SCOUT_SOURCES, default="arxiv", help="Scout source adapter")
    daily_parser.add_argument("--topic", action="append", dest="topics", help="Topic to search; repeatable")
    daily_parser.add_argument("--topic-slot", type=int, choices=(0, 1), default=0, help="Scheduled topic slot: 0 for AM, 1 for PM")
    daily_parser.add_argument(
        "--request-delay",
        type=float,
        default=DEFAULT_ARXIV_REQUEST_DELAY,
        help="Seconds to wait between source topic requests",
    )
    daily_parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_ARXIV_RETRIES,
        help="Retries per source topic request",
    )
    daily_parser.add_argument(
        "--source-timeout",
        type=int,
        default=DEFAULT_ARXIV_TIMEOUT,
        help="Timeout seconds per source request",
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
    pipeline_parser.add_argument("--source", choices=SCOUT_SOURCES, default="arxiv", help="Scout source adapter")
    pipeline_parser.add_argument("--topic", action="append", dest="topics", help="Topic to search; repeatable")
    pipeline_parser.add_argument("--topic-slot", type=int, choices=(0, 1), default=0, help="Scheduled topic slot: 0 for AM, 1 for PM")
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
        help="Seconds to wait between source topic requests",
    )
    pipeline_parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_ARXIV_RETRIES,
        help="Retries per source topic request",
    )
    pipeline_parser.add_argument(
        "--source-timeout",
        type=int,
        default=DEFAULT_ARXIV_TIMEOUT,
        help="Timeout seconds per source request",
    )
    pipeline_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="SQLite database path")
    pipeline_parser.add_argument("--max-scout-attempts", type=int, default=DEFAULT_MAX_SCOUT_ATTEMPTS)
    pipeline_parser.add_argument("--min-quality-score", type=float, default=25.0)
    pipeline_parser.add_argument(
        "--include-seen",
        action="store_true",
        help="Deprecated in V2; previously discovered papers are recorded as excluded Scout candidates",
    )

    rescore_parser = subparsers.add_parser("curator-rescore", help="Preview or apply Curator V2 to existing recommendations")
    rescore_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    rescore_parser.add_argument("--date", required=True, help="Recommendation date in UTC: YYYY-MM-DD")
    rescore_parser.add_argument("--source", choices=SCOUT_SOURCES)
    rescore_parser.add_argument("--apply", action="store_true", help="Persist new scores with an audit of previous values")

    review_parser = subparsers.add_parser("review-summary", help="Create a ChatGPT section-by-section review from a triage summary JSON")
    review_parser.add_argument("summary", type=Path, help="Local extraction summary JSON")
    review_parser.add_argument("--output-dir", type=Path, default=DEFAULT_REVIEW_DIR)
    review_parser.add_argument("--max-chars", type=int, default=DEFAULT_REVIEW_MAX_CHARS)
    review_parser.add_argument("--timeout", type=int, default=240)

    backfill_parser = subparsers.add_parser("review-backfill", help="Extract missing triage summaries for recommended papers")
    backfill_parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="SQLite database path")
    backfill_parser.add_argument("--pdf-dir", type=Path, default=DEFAULT_PDF_DIR)
    backfill_parser.add_argument("--model", default=DEFAULT_MODEL)
    backfill_parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    backfill_parser.add_argument("--max-chars", type=int, default=DEFAULT_PIPELINE_MAX_CHARS)
    backfill_parser.add_argument("--limit-chunks", type=int)
    backfill_parser.add_argument("--quick", action="store_true", help="Extract only the first 2 chunks unless --limit-chunks is set")
    backfill_parser.add_argument("--timeout", type=int, default=DEFAULT_PIPELINE_TIMEOUT)
    backfill_parser.add_argument("--workers", type=int, default=DEFAULT_PIPELINE_WORKERS)
    backfill_parser.add_argument("--limit", type=int, help="Maximum missing summaries to backfill")

    feedback_parser = subparsers.add_parser("feedback", help="Update preferences from feedback")
    feedback_parser.add_argument(
        "feedback_args",
        nargs=argparse.REMAINDER,
        help="Legacy text, or: add/apply feedback subcommands",
    )

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
        if args.db_command == "health":
            summary = health_summary(args.db, days=args.days, source=args.source)
            if args.json:
                print(json.dumps(summary, indent=2, sort_keys=True))
            else:
                print(format_health_summary(summary))
            return
        if args.db_command == "cleanup-legacy-feedback":
            print_section(
                "Legacy feedback cleanup",
                cleanup_legacy_feedback_statuses(args.db, dry_run=not args.yes),
            )
            return

    if args.command == "bootstrap":
        if args.bootstrap_command == "known-papers":
            print_section("Bootstrap known papers", bootstrap_known_papers(db_path=args.db, profile_path=args.profile))
            return
        if args.bootstrap_command == "legacy-scout":
            print_section(
                "Bootstrap legacy Scout files",
                import_legacy_scout_files(
                    args.paths,
                    db_path=args.db,
                    profile_path=args.profile,
                    recommend_all_if_unselected=args.recommend_all_if_unselected,
                ),
            )
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
        base_topics = args.topics if args.topics is not None else select_topics_for_source(
            args.source,
            cadences=("daily", "weekly") if args.source == "openalex" else ("daily",),
            count=2,
            slot=args.topic_slot,
        )
        if not base_topics:
            print("skipped_reason: no_eligible_configured_topics")
            print_section("Daily scout", {"source": args.source, "candidates": [], "skipped_reason": "no_eligible_configured_topics"})
            return
        scout_guidance = None
        scout_guidance_summary = {}
        init_db(args.db)
        with connect_db(args.db) as connection:
            scout_guidance = load_scout_guidance(connection)
            scout_guidance_summary = guidance_summary(scout_guidance)
        topics = topics_with_guidance(base_topics, scout_guidance)
        source_seen_ids = seen_source_ids(args.db, source=args.source) if not args.include_seen else set()
        try:
            output = run_daily_scout(
                topics=topics,
                source=create_scout_source(
                    args.source,
                    request_delay=args.request_delay,
                    retries=args.retries,
                    timeout=args.source_timeout,
                ),
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
                guidance=scout_guidance_summary,
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
                topics=args.topics if args.topics is not None else select_topics_for_source(
                    args.source,
                    cadences=("daily", "weekly") if args.source == "openalex" else ("daily",),
                    count=2,
                    slot=args.topic_slot,
                ),
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
                source_name=args.source,
                topic_slot=args.topic_slot,
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
        if output.get("skipped_reason"):
            print("skipped_reason: no_eligible_configured_topics")
        for scout_result in output.get("scout_results", []):
            guidance = scout_result.get("guidance") or {}
            if guidance:
                print(
                    "Scout guidance: "
                    f"boost={guidance.get('boost_terms', [])[:5]} "
                    f"avoid={guidance.get('avoid_terms', [])[:5]} "
                    f"feedback_count={guidance.get('feedback_count', 0)}"
                )
        print_section("Pipeline review cards", {"cards": output["cards"]})
        return

    if args.command == "curator-rescore":
        try:
            output = rescore_recommendations(args.db, args.date, apply=args.apply, source=args.source)
        except (ValueError, RuntimeError) as error:
            raise SystemExit(str(error)) from error
        print_section("Curator rescore", output)
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

    if args.command == "review-backfill":
        limit_chunks = resolve_pipeline_limit_chunks(args.quick, args.limit_chunks)
        init_db(args.db)
        with connect_db(args.db) as connection:
            output = backfill_missing_triage_summaries(
                connection,
                limit=args.limit,
                config=ReviewerConfig(
                    pdf_dir=args.pdf_dir,
                    model=args.model,
                    ollama_url=args.ollama_url,
                    max_chars=args.max_chars,
                    limit_chunks=limit_chunks,
                    timeout=args.timeout,
                    workers=args.workers,
                ),
            )
        print_section("Review backfill", output)
        return

    if args.command == "feedback":
        if not args.feedback_args:
            raise SystemExit("Feedback text is required")
        if args.feedback_args[0] == "add":
            output = run_feedback_add(args.feedback_args[1:])
            print_section("Feedback ingested", output)
            return
        if args.feedback_args[0] == "apply":
            output = run_feedback_apply(args.feedback_args[1:])
            print_section("Feedback profile apply", output)
            return
        if args.feedback_args[0] == "rebuild-profile":
            output = run_feedback_rebuild_profile(args.feedback_args[1:])
            print_section("Feedback profile rebuild", output)
            return

        require_openai_api_key()
        profile = load_profile(args.profile)
        try:
            updated_profile = FeedbackAgent().run(profile, " ".join(args.feedback_args))
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


def run_feedback_add(argv: list[str]) -> dict[str, Any]:
    parser = argparse.ArgumentParser(prog="paper_agents.cli feedback add")
    parser.add_argument("--paper-id", type=int, required=True)
    parser.add_argument("--recommendation-id", type=int)
    parser.add_argument("--status", choices=["interested", "read_later", "not_interested", "reviewed"])
    parser.add_argument("--file", type=Path)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("text", nargs="*")
    args = parser.parse_args(argv)

    if args.file and args.text:
        raise SystemExit("Use either --file or positional text, not both")
    if args.file:
        content = args.file.read_text(encoding="utf-8")
    else:
        content = " ".join(args.text)
    if not content.strip():
        raise SystemExit("Feedback content is required")

    init_db(args.db)
    with connect_db(args.db) as connection:
        return ingest_feedback_blob(
            connection,
            paper_id=args.paper_id,
            recommendation_id=args.recommendation_id,
            content=content,
            source="cli_feedback_add",
            status=args.status,
        )


def run_feedback_apply(argv: list[str]) -> dict[str, Any]:
    parser = argparse.ArgumentParser(prog="paper_agents.cli feedback apply")
    parser.add_argument("--provider", choices=["gemini"], default="gemini")
    parser.add_argument("--model")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args(argv)

    init_db(args.db)
    with connect_db(args.db) as connection:
        return apply_feedback_to_profile(
            connection,
            provider=args.provider,
            model=args.model,
            limit=args.limit,
            dry_run=args.dry_run,
        )


def run_feedback_rebuild_profile(argv: list[str]) -> dict[str, Any]:
    parser = argparse.ArgumentParser(prog="paper_agents.cli feedback rebuild-profile")
    parser.add_argument("--provider", choices=["gemini"], default="gemini")
    parser.add_argument("--model")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args(argv)

    init_db(args.db)
    with connect_db(args.db) as connection:
        return rebuild_feedback_profile(
            connection,
            provider=args.provider,
            model=args.model,
            limit=args.limit,
            dry_run=args.dry_run,
        )


def print_section(title: str, payload: dict) -> None:
    print(f"\n## {title}")
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def format_health_summary(summary: dict[str, Any]) -> str:
    db_info = summary["db"]
    latest_cycle = summary.get("latest_cycle")
    latest_scout = summary.get("latest_scout_run")
    artifact = summary["artifact_health"]
    feedback = summary["feedback_profile"]
    lines = [
        "## Project Paper health",
        f"DB: {db_info['path']} ({format_bytes(db_info['size_bytes'])}), integrity={db_info['integrity']}",
        f"Range: last {summary['range']['days']} day(s), source={summary['range']['source']}",
    ]
    if latest_cycle:
        lines.append(
            "Latest cycle: "
            f"#{latest_cycle['id']} {latest_cycle['state']} at {latest_cycle['created_at']} "
            f"(age {summary['top']['latest_run_age_hours']}h)"
        )
    else:
        lines.append("Latest cycle: none")
    if latest_scout:
        lines.append(
            "Latest Scout: "
            f"#{latest_scout['id']} {latest_scout['source']} "
            f"candidates={latest_scout['candidate_count']} eligible={latest_scout['eligible_count']} "
            f"excluded={latest_scout['excluded_count']}"
        )
    lines.extend(
        [
            f"Last recommendation day: {summary['latest_recommendation_day'] or 'none'}",
            f"Papers waiting in queue: {summary['top']['papers_waiting_in_queue']}",
            (
                "Artifacts in latest cycle: "
                f"recommendations={artifact['recommendation_count']} "
                f"pdf={artifact['pdf_count']} triage={artifact['triage_summary_count']}"
            ),
            (
                "Feedback/profile: "
                f"unapplied={feedback['unapplied_structured_feedback_count']} "
                f"recent_apply_failures={feedback['recent_apply_failures_count']}"
            ),
            "",
            "Warnings:",
        ]
    )
    if summary["warnings"]:
        lines.extend(f"- [{warning['level']}] {warning['message']}" for warning in summary["warnings"])
    else:
        lines.append("- none")

    if summary["profile_maintenance"]:
        lines.extend(["", "Profile maintenance / Needs attention:"])
        lines.extend(f"- {notice['message']}" for notice in summary["profile_maintenance"])

    lines.extend(["", "Daily Scout funnel:", "day | source | runs | candidates | eligible | excluded"])
    lines.extend(
        _format_table_row(row, ["day", "source", "run_count", "candidate_count", "eligible_count", "excluded_count"])
        for row in summary["daily"]["scout"][:20]
    )
    if not summary["daily"]["scout"]:
        lines.append("none")

    lines.extend(["", "Daily Curator funnel:", "day | source | evaluations | quality_met | recommendations"])
    lines.extend(
        _format_table_row(row, ["day", "source", "evaluation_count", "quality_met_count", "recommendation_count"])
        for row in summary["daily"]["curator"][:20]
    )
    if not summary["daily"]["curator"]:
        lines.append("none")

    lines.extend(["", "Reviewer artifacts:", "day | source | recommendations | pdf | triage"])
    lines.extend(
        _format_table_row(row, ["day", "source", "recommendation_count", "pdf_count", "triage_summary_count"])
        for row in summary["daily"]["reviewer"][:20]
    )
    if not summary["daily"]["reviewer"]:
        lines.append("none")

    lines.extend(["", "Source breakdown:", "source | candidates | eligible | excluded"])
    lines.extend(
        _format_table_row(row, ["source", "candidate_count", "eligible_count", "excluded_count"])
        for row in summary["source_breakdown"]["funnel"]
    )
    if not summary["source_breakdown"]["funnel"]:
        lines.append("none")

    lines.extend(["", "Top exclusion reasons:", "source | reason | count"])
    lines.extend(
        _format_table_row(row, ["source", "reason", "count"])
        for row in summary["source_breakdown"]["exclusion_reasons"][:10]
    )
    if not summary["source_breakdown"]["exclusion_reasons"]:
        lines.append("none")
    return "\n".join(lines)


def _format_table_row(row: dict[str, Any], keys: list[str]) -> str:
    return " | ".join(str(row.get(key) if row.get(key) is not None else 0) for key in keys)


def format_bytes(value: int) -> str:
    size = float(value)
    for unit in ["B", "KiB", "MiB", "GiB"]:
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{value} B"


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
