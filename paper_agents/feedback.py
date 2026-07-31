from __future__ import annotations

import re
from datetime import datetime, timezone
import sqlite3
from typing import Any

from paper_agents import db
from paper_agents.openai_helpers import call_openai_json

DETERMINISTIC_FEEDBACK_PARSER_NAME = "feedback-agent"
DETERMINISTIC_FEEDBACK_PARSER_VERSION = "deterministic-v1"

DECISION_ALIASES = {
    "keep": "keep",
    "maybe": "maybe",
    "reject": "reject",
    "interested": "keep",
    "read later": "maybe",
    "read_later": "maybe",
    "not interested": "reject",
    "not_interested": "reject",
    "reviewed": "keep",
}

STATUS_DECISIONS = {
    "interested": "keep",
    "read_later": "maybe",
    "reviewed": "keep",
    "not_interested": "reject",
}

DECISION_RE = re.compile(r"^\s*decision\s*:\s*(keep|maybe|reject|interested|read\s+later|not\s+interested|reviewed)\s*$", re.IGNORECASE)
SCORE_RE = re.compile(r"^\s*score\s*:\s*([1-5])\s*$", re.IGNORECASE)


class FeedbackAgent:
    """Turns natural-language feedback into an updated preference profile."""

    def run(self, profile: dict[str, Any], feedback_text: str) -> dict[str, Any]:
        system_prompt = """
You are Agent 3: Feedback Agent.
Update the user's research preference profile based on natural-language feedback.
Preserve useful existing interests unless the feedback clearly rejects them.
Keep the profile small and human-editable.
Return only valid JSON in this shape:
{
  "interests": ["..."],
  "positive_signals": ["..."],
  "negative_signals": ["..."],
  "notes": "short profile note",
  "feedback_history": [
    {
      "timestamp": "...",
      "feedback": "...",
      "interpreted_change": "..."
    }
  ]
}
""".strip()
        payload = {
            "current_profile": profile,
            "new_feedback": {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "feedback": feedback_text,
            },
        }
        updated = call_openai_json(system_prompt, payload)
        updated.setdefault("feedback_history", profile.get("feedback_history", []))
        return updated


def ingest_feedback_blob(
    connection: sqlite3.Connection,
    *,
    paper_id: int,
    recommendation_id: int | None,
    content: str,
    source: str,
    status: str | None = None,
) -> dict[str, Any]:
    """Store a pasted feedback blob and deterministically parse durable V2 signals."""
    raw_feedback_id, raw_feedback_created = db.create_raw_feedback(
        connection,
        paper_id=paper_id,
        recommendation_id=recommendation_id,
        content=content,
        metadata={"source": source, "status": status},
    )
    parsed = parse_feedback_blob(content, status=status)
    parse_attempt_id = db.create_feedback_parse_attempt(
        connection,
        raw_feedback_id=raw_feedback_id,
        parser_name=DETERMINISTIC_FEEDBACK_PARSER_NAME,
        parser_version=DETERMINISTIC_FEEDBACK_PARSER_VERSION,
        model=None,
        status="succeeded",
        output=parsed,
    )
    structured_feedback_id = db.create_structured_feedback(
        connection,
        parse_attempt_id=parse_attempt_id,
        paper_id=paper_id,
        decision=parsed["decision"],
        score=parsed["score"],
        observations=parsed["observations"],
        preference_signals=parsed["preference_signals"],
    )
    return {
        "raw_feedback_id": raw_feedback_id,
        "raw_feedback_created": raw_feedback_created,
        "parse_attempt_id": parse_attempt_id,
        "structured_feedback_id": structured_feedback_id,
        "decision": parsed["decision"],
        "score": parsed["score"],
        "observations": parsed["observations"],
        "preference_signals": parsed["preference_signals"],
    }


def parse_feedback_blob(content: str, *, status: str | None = None) -> dict[str, Any]:
    decision = None
    score = None
    observations = []
    for line in content.splitlines():
        decision_match = DECISION_RE.match(line)
        if decision_match:
            decision = normalize_decision(decision_match.group(1))
            continue

        score_match = SCORE_RE.match(line)
        if score_match:
            score = int(score_match.group(1))
            continue

        stripped = line.strip()
        if stripped:
            observations.append(stripped)

    if decision is None and status:
        decision = STATUS_DECISIONS.get(status)

    if not observations and content.strip():
        observations = [content.strip()]

    return {
        "decision": decision,
        "score": score,
        "observations": observations,
        "preference_signals": [],
    }


def normalize_decision(value: str) -> str | None:
    key = " ".join(value.strip().lower().replace("_", " ").split())
    return DECISION_ALIASES.get(key)
