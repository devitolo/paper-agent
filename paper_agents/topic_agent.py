from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from paper_agents.local_extract import DEFAULT_MODEL, DEFAULT_OLLAMA_URL
from paper_agents.scout import SCOUT_SOURCES
from paper_agents.topics import (
    CADENCES,
    PRIORITIES,
    DuplicateTopicError,
    TopicEntry,
    create_topic_from_fast_path,
    ensure_unique_topic,
    find_duplicate_topic,
    normalize_cadence,
    normalize_label,
    normalize_priority,
    normalize_sources,
    update_topic_from_form,
    unique_topic_id,
    validate_topics,
)


TOPIC_ACTIONS = ("create_new", "update_existing", "ask_clarifying_question")
DEFAULT_TOPIC_TIMEOUT_SECONDS = 30


@dataclass
class TopicProposal:
    action: str
    label: str = ""
    query: str = ""
    sources: list[str] | None = None
    cadence: str = "daily"
    priority: str = "normal"
    enabled: bool = True
    matched_topic_id: str | None = None
    rationale: str = ""
    source_rationale: str = ""
    question: str = ""
    provider: str = "deterministic"
    model: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "matched_topic_id": self.matched_topic_id,
            "label": self.label,
            "query": self.query,
            "sources": list(self.sources or []),
            "cadence": self.cadence,
            "priority": self.priority,
            "enabled": bool(self.enabled),
            "rationale": self.rationale,
            "source_rationale": self.source_rationale,
            "question": self.question,
            "provider": self.provider,
            "model": self.model,
        }


TopicProvider = Callable[[str, str, str, int], dict[str, Any]]


def topic_model() -> str:
    return os.environ.get("PAPER_AGENT_TOPIC_MODEL", DEFAULT_MODEL)


def topic_ollama_url() -> str:
    configured = os.environ.get("PAPER_AGENT_TOPIC_OLLAMA_URL") or os.environ.get("PAPER_AGENT_OLLAMA_URL")
    return normalize_ollama_url(configured or DEFAULT_OLLAMA_URL)


def topic_timeout_seconds() -> int:
    raw = os.environ.get("PAPER_AGENT_TOPIC_TIMEOUT_SECONDS")
    if not raw:
        return DEFAULT_TOPIC_TIMEOUT_SECONDS
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_TOPIC_TIMEOUT_SECONDS


def normalize_ollama_url(value: str) -> str:
    cleaned = value.rstrip("/")
    if cleaned.endswith("/api/generate"):
        return cleaned
    return f"{cleaned}/api/generate"


def suggest_topic_proposal(
    user_request: str,
    existing_topics: list[TopicEntry],
    *,
    conversation: list[dict[str, str]] | None = None,
    provider: TopicProvider | None = None,
    model: str | None = None,
    ollama_url: str | None = None,
    timeout: int | None = None,
) -> TopicProposal:
    if not user_request.strip():
        return TopicProposal(
            action="ask_clarifying_question",
            question="What do you want Project Paper to scout?",
            rationale="The request was empty.",
        )

    selected_model = model or topic_model()
    selected_url = ollama_url or topic_ollama_url()
    selected_timeout = timeout or topic_timeout_seconds()
    topic_provider = provider or call_ollama_topic_json
    prompt = build_topic_prompt(user_request, existing_topics, conversation=conversation)
    try:
        raw = topic_provider(selected_url, selected_model, prompt, selected_timeout)
        proposal = validate_topic_proposal(raw, existing_topics, provider="ollama", model=selected_model)
        return redirect_duplicate_create_to_update(proposal, existing_topics)
    except Exception as error:
        return fallback_topic_proposal(user_request, existing_topics, model=selected_model, error=error)


def build_topic_prompt(
    user_request: str,
    existing_topics: list[TopicEntry],
    *,
    conversation: list[dict[str, str]] | None = None,
) -> str:
    existing = [topic.as_dict() for topic in existing_topics]
    turns = normalize_topic_conversation(conversation or [])
    schema = {
        "action": "create_new|update_existing|ask_clarifying_question",
        "matched_topic_id": "existing topic id or null",
        "label": "short human-readable topic label",
        "query": "search query",
        "sources": list(SCOUT_SOURCES),
        "cadence": list(CADENCES),
        "priority": list(PRIORITIES),
        "enabled": True,
        "rationale": "short explanation",
        "source_rationale": "why these sources fit",
        "question": "clarifying question when action is ask_clarifying_question",
    }
    return "\n".join(
        [
            "You are Project Paper TopicAgent.",
            "Project Paper scouts research about AI for SRE, IT operations, observability, incident response, debugging, reliability, infrastructure automation, and engineering workflows.",
            "The user describes what they want Project Paper to scout.",
            "Decide whether to create a new topic, update an existing topic, or ask one clarifying question.",
            "Prefer updating an existing topic when the request is a duplicate or close refinement.",
            "Return strict JSON only. Do not include markdown.",
            f"Allowed sources: {', '.join(SCOUT_SOURCES)}.",
            f"Allowed cadence values: {', '.join(CADENCES)}.",
            f"Allowed priority values: {', '.join(PRIORITIES)}.",
            f"JSON schema: {json.dumps(schema, separators=(',', ':'))}",
            f"Existing topics: {json.dumps(existing, separators=(',', ':'))}",
            f"Conversation so far: {json.dumps(turns, separators=(',', ':'))}",
            f"User request: {user_request.strip()}",
        ]
    )


def call_ollama_topic_json(url: str, model: str, prompt: str, timeout: int) -> dict[str, Any]:
    payload = {
        "model": model,
        "prompt": prompt,
        "format": "json",
        "stream": False,
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    response_text = payload.get("response") if isinstance(payload, dict) else None
    if isinstance(response_text, str):
        return json.loads(response_text)
    if isinstance(payload, dict):
        return payload
    raise ValueError("Ollama response was not JSON")


def validate_topic_proposal(
    raw: dict[str, Any],
    existing_topics: list[TopicEntry],
    *,
    provider: str = "deterministic",
    model: str | None = None,
) -> TopicProposal:
    if not isinstance(raw, dict):
        raise ValueError("Topic proposal must be a JSON object")
    action = str(raw.get("action") or "").strip()
    if action not in TOPIC_ACTIONS:
        raise ValueError(f"Invalid topic action: {action}")
    if action == "ask_clarifying_question":
        question = str(raw.get("question") or "").strip()
        if not question:
            raise ValueError("Clarifying question is required")
        return TopicProposal(
            action=action,
            question=question,
            rationale=str(raw.get("rationale") or "").strip(),
            provider=provider,
            model=model,
        )

    label = normalize_label(str(raw.get("label") or ""))
    query = " ".join(str(raw.get("query") or "").strip().split())
    if not label:
        raise ValueError("Topic label is required")
    if not query:
        raise ValueError("Topic query is required")
    sources = normalize_sources(list(raw.get("sources") or []))
    cadence = normalize_cadence(str(raw.get("cadence") or "daily"))
    priority = normalize_priority(str(raw.get("priority") or "normal"))
    matched_topic_id = raw.get("matched_topic_id")
    if matched_topic_id is not None:
        matched_topic_id = str(matched_topic_id)
    if action == "update_existing" and not topic_by_id(existing_topics, matched_topic_id):
        raise ValueError(f"Matched topic does not exist: {matched_topic_id}")

    topic = TopicProposal(
        action=action,
        matched_topic_id=matched_topic_id,
        label=label,
        query=query,
        sources=sources,
        cadence=cadence,
        priority=priority,
        enabled=parse_enabled(raw.get("enabled", True)),
        rationale=str(raw.get("rationale") or "").strip(),
        source_rationale=str(raw.get("source_rationale") or "").strip(),
        provider=provider,
        model=model,
    )
    validate_topics(
        [
            TopicEntry(
                id=matched_topic_id or "proposal",
                label=topic.label,
                query=topic.query,
                sources=topic.sources or [],
                cadence=topic.cadence,
                priority=topic.priority,
                enabled=topic.enabled,
            ).as_dict()
        ]
    )
    return topic


def redirect_duplicate_create_to_update(proposal: TopicProposal, existing_topics: list[TopicEntry]) -> TopicProposal:
    if proposal.action != "create_new":
        return proposal
    duplicate = find_duplicate_topic(proposal_to_topic_entry(proposal, existing_topics), existing_topics)
    if duplicate is None:
        return proposal
    proposal.action = "update_existing"
    proposal.matched_topic_id = duplicate.id
    if not proposal.rationale:
        proposal.rationale = f"Matched existing topic: {duplicate.label}."
    return proposal


def parse_enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"false", "0", "no", "off", "disabled"}:
            return False
        if normalized in {"true", "1", "yes", "on", "enabled"}:
            return True
    return bool(value)


def normalize_topic_conversation(raw_turns: list[dict[str, Any]]) -> list[dict[str, str]]:
    turns: list[dict[str, str]] = []
    for raw in raw_turns[-10:]:
        role = str(raw.get("role") or "").strip().casefold()
        if role not in {"user", "agent"}:
            continue
        content = " ".join(str(raw.get("content") or "").strip().split())
        if not content:
            continue
        turns.append({"role": role, "content": content[:1000]})
    return turns


def fallback_topic_proposal(
    user_request: str,
    existing_topics: list[TopicEntry],
    *,
    model: str | None = None,
    error: Exception | None = None,
) -> TopicProposal:
    reason = fallback_reason(error)
    try:
        topic = create_topic_from_fast_path(user_request, existing_topics=existing_topics)
        return TopicProposal(
            action="create_new",
            label=topic.label,
            query=topic.query,
            sources=topic.sources,
            cadence=topic.cadence,
            priority=topic.priority,
            enabled=topic.enabled,
            rationale=f"Generated deterministically because local Qwen/Ollama was unavailable or returned invalid JSON. {reason}",
            source_rationale="Defaulted to all active sources.",
            provider="deterministic_fallback",
            model=model,
        )
    except DuplicateTopicError as duplicate_error:
        return TopicProposal(
            action="update_existing",
            matched_topic_id=duplicate_error.topic.id,
            label=duplicate_error.topic.label,
            query=duplicate_error.topic.query,
            sources=duplicate_error.topic.sources,
            cadence=duplicate_error.topic.cadence,
            priority=duplicate_error.topic.priority,
            enabled=duplicate_error.topic.enabled,
            rationale=f"Matched an existing topic deterministically because local Qwen/Ollama was unavailable or returned invalid JSON. {reason}",
            source_rationale="Preserved existing source selection.",
            provider="deterministic_fallback",
            model=model,
        )
    except Exception:
        label = normalize_label(user_request)
        query = " ".join(str(user_request).strip().split())
        candidate = TopicEntry("proposal", label or "Topic", query or "topic", list(SCOUT_SOURCES))
        duplicate = find_duplicate_topic(candidate, existing_topics)
        if duplicate is None and label:
            duplicate = topic_by_label(existing_topics, label)
        if duplicate is None:
            raise
        return TopicProposal(
            action="update_existing",
            matched_topic_id=duplicate.id,
            label=duplicate.label,
            query=duplicate.query,
            sources=duplicate.sources,
            cadence=duplicate.cadence,
            priority=duplicate.priority,
            enabled=duplicate.enabled,
            rationale=f"Matched an existing topic deterministically because local Qwen/Ollama was unavailable or returned invalid JSON. {reason}",
            source_rationale="Preserved existing source selection.",
            provider="deterministic_fallback",
            model=model,
        )


def fallback_reason(error: Exception | None) -> str:
    if error is None:
        return ""
    detail = str(error).strip()
    if not detail:
        detail = error.__class__.__name__
    detail = " ".join(detail.split())
    if len(detail) > 140:
        detail = detail[:137].rstrip() + "..."
    return f"Fallback reason: {detail}"


def apply_topic_proposal(proposal: TopicProposal, existing_topics: list[TopicEntry]) -> list[TopicEntry]:
    if proposal.action == "ask_clarifying_question":
        raise ValueError("Clarifying question proposals cannot be applied")
    if proposal.action == "create_new":
        topic = proposal_to_topic_entry(proposal, existing_topics)
        duplicate = find_duplicate_topic(topic, existing_topics)
        if duplicate is not None:
            proposal = TopicProposal(
                action="update_existing",
                matched_topic_id=duplicate.id,
                label=proposal.label,
                query=proposal.query,
                sources=proposal.sources,
                cadence=proposal.cadence,
                priority=proposal.priority,
                enabled=proposal.enabled,
                rationale=proposal.rationale,
                source_rationale=proposal.source_rationale,
                provider=proposal.provider,
                model=proposal.model,
            )
        else:
            return [*existing_topics, topic]
    if proposal.action == "update_existing":
        for index, topic in enumerate(existing_topics):
            if topic.id != proposal.matched_topic_id:
                continue
            updated = update_topic_from_form(
                topic,
                label=proposal.label,
                query=proposal.query,
                sources=proposal.sources or [],
                cadence=proposal.cadence,
                priority=proposal.priority,
                enabled=proposal.enabled,
            )
            candidate_topics = list(existing_topics)
            candidate_topics[index] = updated
            validate_topics([item.as_dict() for item in candidate_topics])
            ensure_unique_topic(updated, existing_topics, ignore_id=topic.id)
            return candidate_topics
        raise ValueError(f"Matched topic does not exist: {proposal.matched_topic_id}")
    raise ValueError(f"Unsupported proposal action: {proposal.action}")


def proposal_to_topic_entry(proposal: TopicProposal, existing_topics: list[TopicEntry]) -> TopicEntry:
    topic = TopicEntry(
        id=unique_topic_id(proposal.label, existing_topics),
        label=proposal.label,
        query=proposal.query,
        sources=normalize_sources(proposal.sources or []),
        cadence=normalize_cadence(proposal.cadence),
        priority=normalize_priority(proposal.priority),
        enabled=proposal.enabled,
    )
    validate_topics([topic.as_dict()])
    return topic


def topic_by_id(topics: list[TopicEntry], topic_id: str | None) -> TopicEntry | None:
    return next((topic for topic in topics if topic.id == topic_id), None)


def topic_by_label(topics: list[TopicEntry], label: str) -> TopicEntry | None:
    normalized = normalize_label(label).casefold()
    return next((topic for topic in topics if topic.label.casefold() == normalized), None)


def topic_proposal_from_json(value: str, existing_topics: list[TopicEntry]) -> TopicProposal:
    return validate_topic_proposal(json.loads(value), existing_topics)


def topic_proposal_to_json(proposal: TopicProposal) -> str:
    return json.dumps(proposal.as_dict(), separators=(",", ":"))
