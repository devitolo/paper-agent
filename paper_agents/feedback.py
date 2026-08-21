from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
import sqlite3
from typing import Any, Callable

from paper_agents import db
from paper_agents.openai_helpers import call_openai_json

DETERMINISTIC_FEEDBACK_PARSER_NAME = "feedback-agent"
DETERMINISTIC_FEEDBACK_PARSER_VERSION = "deterministic-v1"
DEFAULT_GEMINI_TIMEOUT_SECONDS = 180
GEMINI_TIMEOUT_ENV = "PAPER_AGENT_GEMINI_TIMEOUT_SECONDS"
GEMINI_FLASH_LITE_FALLBACK_MODEL = "gemini-3.1-flash-lite"

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
SCORE_RE = re.compile(r"^\s*score\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*$", re.IGNORECASE)
ProfileProvider = Callable[[dict[str, Any], str | None], dict[str, Any]]


class ProfileProviderFailure(RuntimeError):
    def __init__(self, message: str, *, model: str | None) -> None:
        super().__init__(message)
        self.model = model


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
            parsed_score = float(score_match.group(1))
            if 1 <= parsed_score <= 5:
                score = parsed_score
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


def apply_feedback_to_profile(
    connection: sqlite3.Connection,
    *,
    provider: str = "gemini",
    model: str | None = None,
    limit: int | None = None,
    structured_feedback_ids: list[int] | None = None,
    dry_run: bool = True,
    provider_fn: ProfileProvider | None = None,
) -> dict[str, Any]:
    if provider != "gemini":
        raise ValueError(f"Unsupported feedback profile provider: {provider}")

    feedback_rows = (
        db.structured_feedback_by_ids(connection, structured_feedback_ids)
        if structured_feedback_ids is not None
        else db.unapplied_structured_feedback(connection, limit=limit)
    )
    feedback_ids = [row["id"] for row in feedback_rows]
    current = db.current_profile_version(connection)
    current_profile = current["profile"] if current else {}
    if not feedback_rows:
        return {
            "status": "no_feedback",
            "dry_run": dry_run,
            "provider": provider,
            "model": model,
            "current_profile_version_id": current["id"] if current else None,
            "structured_feedback_ids": [],
            "message": "No structured feedback rows found for profile apply.",
        }

    payload = {
        "current_profile": current_profile,
        "structured_feedback": feedback_rows,
    }
    try:
        proposed, effective_model = propose_profile_update(
            connection,
            payload=payload,
            provider=provider,
            model=model,
            dry_run=dry_run,
            feedback_ids=feedback_ids,
            mode="incremental",
            provider_fn=provider_fn,
        )
    except ProfileProviderFailure as error:
        attempt_id = db.create_feedback_profile_apply_attempt(
            connection,
            provider=provider,
            model=error.model,
            structured_feedback_ids=feedback_ids,
            dry_run=dry_run,
            status="failed",
            error=str(error),
            metadata={"mode": "incremental"},
        )
        return {
            "status": "failed",
            "dry_run": dry_run,
            "provider": provider,
            "model": error.model,
            "current_profile_version_id": current["id"] if current else None,
            "structured_feedback_ids": feedback_ids,
            "error": str(error),
            "apply_attempt_id": attempt_id,
        }
    output = {
        "status": "dry_run" if dry_run else "applied",
        "dry_run": dry_run,
        "provider": provider,
        "model": effective_model,
        "current_profile_version_id": current["id"] if current else None,
        "structured_feedback_ids": feedback_ids,
        "proposed_profile": proposed["profile"],
        "change_summary": proposed["change_summary"],
    }
    if dry_run:
        attempt_id = db.create_feedback_profile_apply_attempt(
            connection,
            provider=provider,
            model=effective_model,
            structured_feedback_ids=feedback_ids,
            dry_run=True,
            status="succeeded",
            metadata={"mode": "incremental", "change_summary": proposed["change_summary"]},
        )
        output["apply_attempt_id"] = attempt_id
        return output

    profile_version_id = db.create_profile_version(
        connection,
        proposed["profile"],
        source_structured_feedback_id=feedback_ids[0],
        change_summary=proposed["change_summary"],
    )
    application_ids = db.create_feedback_profile_applications(connection, feedback_ids, profile_version_id)
    attempt_id = db.create_feedback_profile_apply_attempt(
        connection,
        provider=provider,
        model=effective_model,
        structured_feedback_ids=feedback_ids,
        dry_run=False,
        status="succeeded",
        profile_version_id=profile_version_id,
        metadata={"mode": "incremental", "change_summary": proposed["change_summary"]},
    )
    output["profile_version_id"] = profile_version_id
    output["feedback_profile_application_ids"] = application_ids
    output["apply_attempt_id"] = attempt_id
    return output


def rebuild_feedback_profile(
    connection: sqlite3.Connection,
    *,
    provider: str = "gemini",
    model: str | None = None,
    limit: int | None = None,
    dry_run: bool = True,
    provider_fn: ProfileProvider | None = None,
) -> dict[str, Any]:
    if provider != "gemini":
        raise ValueError(f"Unsupported feedback profile provider: {provider}")

    feedback_rows = db.all_structured_feedback(connection, limit=limit)
    feedback_ids = [row["id"] for row in feedback_rows]
    current = db.current_profile_version(connection)
    current_profile = current["profile"] if current else {}
    if not feedback_rows:
        return {
            "status": "no_feedback",
            "dry_run": dry_run,
            "provider": provider,
            "model": model,
            "current_profile_version_id": current["id"] if current else None,
            "structured_feedback_ids": [],
            "message": "No structured feedback rows found for profile rebuild.",
        }

    payload = {
        "mode": "full_rebuild",
        "current_profile": current_profile,
        "structured_feedback": feedback_rows,
    }
    try:
        proposed, effective_model = propose_profile_update(
            connection,
            payload=payload,
            provider=provider,
            model=model,
            dry_run=dry_run,
            feedback_ids=feedback_ids,
            mode="full_rebuild",
            provider_fn=provider_fn,
        )
    except ProfileProviderFailure as error:
        attempt_id = db.create_feedback_profile_apply_attempt(
            connection,
            provider=provider,
            model=error.model,
            structured_feedback_ids=feedback_ids,
            dry_run=dry_run,
            status="failed",
            error=str(error),
            metadata={"mode": "full_rebuild"},
        )
        return {
            "status": "failed",
            "dry_run": dry_run,
            "provider": provider,
            "model": error.model,
            "current_profile_version_id": current["id"] if current else None,
            "structured_feedback_ids": feedback_ids,
            "error": str(error),
            "apply_attempt_id": attempt_id,
        }
    output = {
        "status": "dry_run" if dry_run else "rebuilt",
        "dry_run": dry_run,
        "provider": provider,
        "model": effective_model,
        "current_profile_version_id": current["id"] if current else None,
        "structured_feedback_ids": feedback_ids,
        "proposed_profile": proposed["profile"],
        "change_summary": proposed["change_summary"],
    }
    if dry_run:
        attempt_id = db.create_feedback_profile_apply_attempt(
            connection,
            provider=provider,
            model=effective_model,
            structured_feedback_ids=feedback_ids,
            dry_run=True,
            status="succeeded",
            metadata={"mode": "full_rebuild", "change_summary": proposed["change_summary"]},
        )
        output["apply_attempt_id"] = attempt_id
        return output

    profile_version_id = db.create_profile_version(
        connection,
        proposed["profile"],
        source_structured_feedback_id=None,
        change_summary=proposed["change_summary"],
    )
    attempt_id = db.create_feedback_profile_apply_attempt(
        connection,
        provider=provider,
        model=effective_model,
        structured_feedback_ids=feedback_ids,
        dry_run=False,
        status="succeeded",
        profile_version_id=profile_version_id,
        metadata={"mode": "full_rebuild", "change_summary": proposed["change_summary"]},
    )
    output["profile_version_id"] = profile_version_id
    output["apply_attempt_id"] = attempt_id
    return output


def propose_profile_update(
    connection: sqlite3.Connection,
    *,
    payload: dict[str, Any],
    provider: str,
    model: str | None,
    dry_run: bool,
    feedback_ids: list[int],
    mode: str,
    provider_fn: ProfileProvider | None = None,
) -> tuple[dict[str, Any], str | None]:
    profile_provider = provider_fn or call_gemini_json
    try:
        return normalize_profile_update(profile_provider(payload, model)), model
    except RuntimeError as error:
        if not should_fallback_to_flash_lite(error, model=model, provider_fn=provider_fn):
            raise ProfileProviderFailure(str(error), model=model) from error

        db.create_feedback_profile_apply_attempt(
            connection,
            provider=provider,
            model=model,
            structured_feedback_ids=feedback_ids,
            dry_run=dry_run,
            status="failed",
            error=str(error),
            metadata={"mode": mode, "fallback_model": GEMINI_FLASH_LITE_FALLBACK_MODEL},
        )
        try:
            proposed = normalize_profile_update(profile_provider(payload, GEMINI_FLASH_LITE_FALLBACK_MODEL))
        except RuntimeError as fallback_error:
            raise ProfileProviderFailure(str(fallback_error), model=GEMINI_FLASH_LITE_FALLBACK_MODEL) from fallback_error
        return proposed, GEMINI_FLASH_LITE_FALLBACK_MODEL


def should_fallback_to_flash_lite(error: RuntimeError, *, model: str | None, provider_fn: ProfileProvider | None) -> bool:
    if provider_fn is not None or model is not None:
        return False
    message = str(error).lower()
    return any(
        marker in message
        for marker in [
            "429",
            "quota",
            "rate limit",
            "ratelimit",
            "too many requests",
            "toomanyrequests",
            "free_tier_requests",
            "exhausted",
        ]
    )


def call_gemini_json(payload: dict[str, Any], model: str | None = None) -> dict[str, Any]:
    prompt = build_profile_update_prompt(payload)
    command = ["gemini"]
    if model:
        command.extend(["--model", model])
    command.extend(["-p", prompt])
    timeout = gemini_timeout_seconds()
    try:
        result = subprocess.run(command, text=True, capture_output=True, check=True, timeout=timeout)
    except FileNotFoundError as error:
        raise RuntimeError("Gemini CLI was not found. Install and authenticate `gemini`, then retry.") from error
    except subprocess.CalledProcessError as error:
        details = error.stderr.strip() or error.stdout.strip()
        raise RuntimeError(f"Gemini CLI failed: {details}") from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"Gemini CLI timed out after {timeout} seconds while updating the feedback profile.") from error
    return parse_json_object(result.stdout)


def gemini_timeout_seconds() -> int:
    value = os.environ.get(GEMINI_TIMEOUT_ENV)
    if not value:
        return DEFAULT_GEMINI_TIMEOUT_SECONDS
    try:
        parsed = int(value)
    except ValueError:
        return DEFAULT_GEMINI_TIMEOUT_SECONDS
    return max(1, parsed)


def build_profile_update_prompt(payload: dict[str, Any]) -> str:
    rebuild_note = (
        "This is a full rebuild: compress all repeated feedback into durable preferences and avoid appending paper-specific details endlessly.\n"
        if payload.get("mode") == "full_rebuild"
        else ""
    )
    return (
        "You are Project Paper's Feedback Agent.\n"
        "Update the user's small, human-editable research preference profile from structured feedback.\n"
        f"{rebuild_note}"
        "Preserve useful existing preferences unless feedback clearly rejects them.\n"
        "Return valid JSON only in this exact shape:\n"
        "{\n"
        '  "profile": {\n'
        '    "interests": ["..."],\n'
        '    "positive_signals": ["..."],\n'
        '    "negative_signals": ["..."],\n'
        '    "notes": "..."\n'
        "  },\n"
        '  "change_summary": "short human-readable summary"\n'
        "}\n\n"
        f"Input:\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


def parse_json_object(value: str) -> dict[str, Any]:
    text = value.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise RuntimeError("Provider did not return valid JSON.") from error
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError as second_error:
            raise RuntimeError("Provider did not return valid JSON.") from second_error
    if not isinstance(parsed, dict):
        raise RuntimeError("Provider JSON response must be an object.")
    return parsed


def normalize_profile_update(value: dict[str, Any]) -> dict[str, Any]:
    profile = value.get("profile")
    if not isinstance(profile, dict):
        raise RuntimeError("Provider JSON response must include a profile object.")
    normalized_profile = {
        "interests": normalize_string_list(profile.get("interests")),
        "positive_signals": normalize_string_list(profile.get("positive_signals")),
        "negative_signals": normalize_string_list(profile.get("negative_signals")),
        "notes": str(profile.get("notes") or "").strip(),
    }
    change_summary = str(value.get("change_summary") or "").strip()
    if not change_summary:
        change_summary = "Updated profile from structured feedback."
    return {"profile": normalized_profile, "change_summary": change_summary}


def normalize_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
