from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from paper_agents.curator import ResearchCurator
from paper_agents.feedback import FeedbackAgent
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
