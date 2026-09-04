#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${PAPER_AGENT_REPO:-$HOME/workspace/paper-agent}"
DB_PATH="${PAPER_AGENT_DB:-data/paper_agent.db}"
MODEL="${PAPER_AGENT_GEMINI_MODEL:-}"

cd "$REPO_DIR"

args=(--db "$DB_PATH")
if [[ -n "$MODEL" ]]; then
  args+=(--model "$MODEL")
fi

python3 - "${args[@]}" <<'PY'
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from paper_agents import db
from paper_agents.feedback import rebuild_feedback_profile


def main() -> None:
    parser = argparse.ArgumentParser(description="Preview and compare a Gemini full feedback-profile rebuild.")
    parser.add_argument("--db", default="data/paper_agent.db")
    parser.add_argument("--model")
    args = parser.parse_args()

    db_path = Path(args.db)
    db.init_db(db_path)
    with db.connect_db(db_path) as connection:
        current = db.current_profile_version(connection)
        current_profile = current["profile"] if current else {}
        result = rebuild_feedback_profile(
            connection,
            provider="gemini",
            model=args.model,
            dry_run=True,
        )

    print("== Project Paper Gemini profile rebuild comparison ==")
    print(f"DB: {db_path}")
    print(f"Current profile version: {result.get('current_profile_version_id') or 'none'}")
    print(f"Status: {result.get('status')}")
    print(f"Model: {result.get('model') or args.model or 'default'}")
    print(f"Structured feedback rows: {len(result.get('structured_feedback_ids') or [])}")
    if result.get("apply_attempt_id"):
        print(f"Dry-run attempt id: {result['apply_attempt_id']}")

    if result.get("status") != "dry_run":
        print("")
        print(result.get("message") or result.get("error") or "No proposed profile was produced.")
        return

    proposed_profile = result.get("proposed_profile") or {}
    print("")
    print("Change summary:")
    print(result.get("change_summary") or "No change summary returned.")
    print("")
    print("Profile section comparison:")
    for key in sorted(set(current_profile) | set(proposed_profile)):
        print_section_diff(key, current_profile.get(key), proposed_profile.get(key))

    print("")
    print("Current profile JSON:")
    print(json.dumps(current_profile, indent=2, ensure_ascii=False, sort_keys=True))
    print("")
    print("Proposed profile JSON:")
    print(json.dumps(proposed_profile, indent=2, ensure_ascii=False, sort_keys=True))


def print_section_diff(key: str, current: Any, proposed: Any) -> None:
    current_items = normalized_items(current)
    proposed_items = normalized_items(proposed)
    if current_items or proposed_items:
        added = [item for item in proposed_items if item not in current_items]
        removed = [item for item in current_items if item not in proposed_items]
        kept = [item for item in proposed_items if item in current_items]
        print(f"- {key}: {len(kept)} kept, {len(added)} added, {len(removed)} removed")
        if added:
            print(f"  + {', '.join(added[:12])}")
        if removed:
            print(f"  - {', '.join(removed[:12])}")
        return

    if current != proposed:
        print(f"- {key}: changed")
        print(f"  current: {compact_value(current)}")
        print(f"  proposed: {compact_value(proposed)}")
    else:
        print(f"- {key}: unchanged")


def normalized_items(value: Any) -> list[str]:
    if isinstance(value, list):
        return [normalize(item) for item in value if normalize(item)]
    if isinstance(value, dict):
        return [normalize(key) for key in value if normalize(key)]
    return []


def normalize(value: Any) -> str:
    return " ".join(str(value).strip().split())


def compact_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


if __name__ == "__main__":
    main()
PY
