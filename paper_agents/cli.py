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
from paper_agents.scout import ResearchScout
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
        output_path.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n")
        print(f"wrote {output_path}")
        print_section("Local extraction", output["merged"])
        return

    if args.command == "profile":
        print(json.dumps(load_profile(args.profile), indent=2, ensure_ascii=False))


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
