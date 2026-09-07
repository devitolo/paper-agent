from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from paper_agents.scout import DEFAULT_SCOUT_TOPICS, SCOUT_SOURCES


DEFAULT_TOPIC_CONFIG_PATH = Path("config/topics.yaml")
TOPIC_SOURCE_ORDER = ("arxiv", "openalex", "semantic_scholar")
CADENCES = ("daily", "weekly", "manual")
PRIORITIES = ("high", "normal", "low")
PRIORITY_RANK = {"high": 0, "normal": 1, "low": 2}
SCHEDULED_TOPICS_PER_RUN = 2
SCHEDULED_RUNS_PER_DAY = 2
BASELINE_QUERY_TERMS = ["operations", "reliability", "observability", "production engineering"]


@dataclass
class TopicEntry:
    id: str
    label: str
    query: str
    sources: list[str]
    cadence: str = "daily"
    priority: str = "normal"
    enabled: bool = True
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "label": self.label,
            "query": self.query,
            "sources": list(self.sources),
            "cadence": self.cadence,
            "priority": self.priority,
            "enabled": self.enabled,
        }
        if self.note:
            data["note"] = self.note
        return data


class DuplicateTopicError(ValueError):
    def __init__(self, topic: TopicEntry):
        self.topic = topic
        super().__init__(f"Topic already exists: {topic.label}")


def load_topic_config(path: Path = DEFAULT_TOPIC_CONFIG_PATH) -> list[TopicEntry]:
    if not path.exists():
        raise FileNotFoundError(path)
    try:
        parsed = parse_topics_yaml(path.read_text(encoding="utf-8"))
        return validate_topics(parsed)
    except Exception as error:
        raise ValueError(f"Invalid topic config {path}: {error}") from error


def load_topic_config_or_seed(path: Path = DEFAULT_TOPIC_CONFIG_PATH) -> list[TopicEntry]:
    try:
        return load_topic_config(path)
    except (FileNotFoundError, ValueError):
        return seed_topic_entries()


def save_topic_config(topics: list[TopicEntry], path: Path = DEFAULT_TOPIC_CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = render_topics_yaml(validate_topics([topic.as_dict() for topic in topics]))
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(content)
        temp_path = Path(handle.name)
    temp_path.replace(path)


def select_topics_for_source(
    source: str,
    *,
    today: date | None = None,
    cadences: tuple[str, ...] = ("daily",),
    path: Path = DEFAULT_TOPIC_CONFIG_PATH,
    fallback_topics: list[str] | None = None,
    count: int = 1,
    slot: int = 0,
) -> list[str]:
    fallback = list(DEFAULT_SCOUT_TOPICS if fallback_topics is None else fallback_topics)
    try:
        topics = load_topic_config(path)
    except (FileNotFoundError, ValueError):
        return fallback

    candidates = [
        topic
        for topic in topics
        if topic.enabled and source in topic.sources and topic.cadence in cadences
    ]
    if not candidates:
        return fallback

    current = today or date.today()
    high_water = min(PRIORITY_RANK.get(topic.priority, 9) for topic in candidates)
    priority_pool = [topic for topic in candidates if PRIORITY_RANK.get(topic.priority, 9) == high_water]
    batch_size = max(1, count)
    run_slot = max(0, slot)
    batch_start = ((current.toordinal() * SCHEDULED_RUNS_PER_DAY + run_slot) * batch_size) % len(priority_pool)
    return [priority_pool[(batch_start + offset) % len(priority_pool)].query for offset in range(min(batch_size, len(priority_pool)))]


def topic_inventory_from_config(path: Path = DEFAULT_TOPIC_CONFIG_PATH, today: date | None = None) -> list[dict[str, Any]]:
    topics = load_topic_config_or_seed(path)
    current = today or date.today()
    inventory = []
    for source in TOPIC_SOURCE_ORDER:
        source_topics = [topic for topic in topics if source in topic.sources]
        active_query = select_topics_for_source(
            source,
            today=current,
            cadences=("daily", "weekly") if source == "openalex" else ("daily",),
            path=path,
            fallback_topics=[],
            count=SCHEDULED_TOPICS_PER_RUN,
        )
        inventory.append(
            {
                "source": source,
                "label": source_display_name(source),
                "schedule": schedule_label(source),
                "description": schedule_description(source),
                "topics": source_topics,
                "active_topics": active_query,
                "notes": source_notes(source),
            }
        )
    return inventory


def create_topic_from_fast_path(
    text: str,
    *,
    existing_topics: list[TopicEntry] | None = None,
    sources: list[str] | None = None,
    query: str | None = None,
    cadence: str = "daily",
    priority: str = "normal",
    enabled: bool = True,
) -> TopicEntry:
    label = normalize_label(text)
    if not label:
        raise ValueError("Topic label is required")
    selected_sources = normalize_sources(sources or list(SCOUT_SOURCES))
    entry = TopicEntry(
        id=unique_topic_id(label, existing_topics or []),
        label=label,
        query=(query or default_query_for_topic(label)).strip(),
        sources=selected_sources,
        cadence=normalize_cadence(cadence),
        priority=normalize_priority(priority),
        enabled=bool(enabled),
    )
    validate_topics([entry.as_dict()])
    ensure_unique_topic(entry, existing_topics or [])
    return entry


def update_topic_from_form(
    topic: TopicEntry,
    *,
    label: str,
    query: str,
    sources: list[str],
    cadence: str,
    priority: str,
    enabled: bool,
) -> TopicEntry:
    updated = TopicEntry(
        id=topic.id,
        label=normalize_label(label),
        query=query.strip(),
        sources=normalize_sources(sources),
        cadence=normalize_cadence(cadence),
        priority=normalize_priority(priority),
        enabled=enabled,
        note=topic.note,
    )
    validate_topics([updated.as_dict()])
    return updated


def ensure_unique_topic(topic: TopicEntry, existing_topics: list[TopicEntry], *, ignore_id: str | None = None) -> None:
    duplicate = find_duplicate_topic(topic, existing_topics, ignore_id=ignore_id)
    if duplicate is not None:
        raise DuplicateTopicError(duplicate)


def find_duplicate_topic(
    topic: TopicEntry,
    existing_topics: list[TopicEntry],
    *,
    ignore_id: str | None = None,
) -> TopicEntry | None:
    topic_label = normalize_topic_match_value(topic.label)
    topic_query = normalize_topic_match_value(topic.query)
    topic_slug = slug_for_topic_id(topic.label)
    for existing in existing_topics:
        if existing.id == ignore_id:
            continue
        if existing.id == topic.id or existing.id == topic_slug:
            return existing
        if normalize_topic_match_value(existing.label) == topic_label:
            return existing
        if normalize_topic_match_value(existing.query) == topic_query:
            return existing
    return None


def seed_topic_entries() -> list[TopicEntry]:
    topics = [
        TopicEntry(
            id=unique_topic_id(topic, []),
            label=topic,
            query=topic,
            sources=["arxiv"],
            cadence="daily",
            priority="normal",
            enabled=True,
            note="Seeded from historical arXiv defaults.",
        )
        for topic in DEFAULT_SCOUT_TOPICS
    ]
    topics.extend(
        [
            TopicEntry(
                id="openalex-aiops-observability-incident-response",
                label="AIOps observability incident response",
                query="AIOps observability incident response",
                sources=["openalex"],
                cadence="weekly",
                priority="normal",
                enabled=True,
                note="Seeded from the OpenAlex rotating script.",
            ),
            TopicEntry(
                id="openalex-cloud-operations-anomaly-detection-remediation",
                label="Cloud operations anomaly detection remediation",
                query="cloud operations anomaly detection remediation",
                sources=["openalex"],
                cadence="weekly",
                priority="normal",
                enabled=True,
                note="Seeded from the OpenAlex rotating script.",
            ),
            TopicEntry(
                id="openalex-microservice-diagnosis-distributed-systems-debugging",
                label="Microservice diagnosis distributed systems debugging",
                query="microservice diagnosis distributed systems debugging",
                sources=["openalex"],
                cadence="weekly",
                priority="normal",
                enabled=True,
                note="Seeded from the OpenAlex rotating script.",
            ),
            TopicEntry(
                id="openalex-software-reliability-engineering-production-incidents",
                label="Software reliability engineering production incidents",
                query="software reliability engineering production incidents",
                sources=["openalex"],
                cadence="weekly",
                priority="normal",
                enabled=True,
                note="Seeded from the OpenAlex rotating script.",
            ),
            TopicEntry(
                id="openalex-llm-operations-root-cause-analysis-logs-traces",
                label="LLM operations root cause analysis logs traces",
                query="LLM operations root cause analysis logs traces",
                sources=["openalex"],
                cadence="weekly",
                priority="normal",
                enabled=True,
                note="Seeded from the OpenAlex rotating script.",
            ),
            TopicEntry(
                id="semantic-scholar-aiops-root-cause-analysis",
                label="AIOps root cause analysis",
                query="AIOps root cause analysis",
                sources=["semantic_scholar"],
                cadence="daily",
                priority="normal",
                enabled=True,
                note="Seeded from the documented Semantic Scholar cron topic.",
            ),
        ]
    )
    return topics


def validate_topics(raw_topics: list[dict[str, Any] | TopicEntry]) -> list[TopicEntry]:
    entries = [topic if isinstance(topic, TopicEntry) else TopicEntry(**topic) for topic in raw_topics]
    seen_ids: set[str] = set()
    for topic in entries:
        if not topic.id or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", topic.id):
            raise ValueError(f"Invalid topic id: {topic.id}")
        if topic.id in seen_ids:
            raise ValueError(f"Duplicate topic id: {topic.id}")
        seen_ids.add(topic.id)
        if not topic.label.strip():
            raise ValueError(f"Topic {topic.id} is missing a label")
        if not topic.query.strip():
            raise ValueError(f"Topic {topic.id} is missing a query")
        topic.sources = normalize_sources(topic.sources)
        topic.cadence = normalize_cadence(topic.cadence)
        topic.priority = normalize_priority(topic.priority)
        topic.enabled = bool(topic.enabled)
    return entries


def parse_topics_yaml(content: str) -> list[dict[str, Any]]:
    topics: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    in_topics = False
    for raw_line in content.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        if stripped == "topics:":
            in_topics = True
            continue
        if not in_topics:
            continue
        if stripped.startswith("- "):
            current = {}
            topics.append(current)
            remainder = stripped[2:].strip()
            if remainder:
                key, value = split_yaml_pair(remainder)
                current[key] = parse_yaml_value(value)
            continue
        if current is None:
            raise ValueError("Found topic field before first list item")
        key, value = split_yaml_pair(stripped)
        current[key] = parse_yaml_value(value)
    return topics


def render_topics_yaml(topics: list[TopicEntry]) -> str:
    lines = [
        "# Project Paper Scout topics.",
        "# Edit through /topics when possible; this file is intentionally human-readable.",
        "topics:",
    ]
    for topic in topics:
        lines.extend(
            [
                f"  - id: {quote_yaml(topic.id)}",
                f"    label: {quote_yaml(topic.label)}",
                f"    query: {quote_yaml(topic.query)}",
                f"    sources: [{', '.join(topic.sources)}]",
                f"    cadence: {topic.cadence}",
                f"    priority: {topic.priority}",
                f"    enabled: {str(topic.enabled).lower()}",
            ]
        )
        if topic.note:
            lines.append(f"    note: {quote_yaml(topic.note)}")
    return "\n".join(lines) + "\n"


def split_yaml_pair(text: str) -> tuple[str, str]:
    if ":" not in text:
        raise ValueError(f"Invalid YAML field: {text}")
    key, value = text.split(":", 1)
    return key.strip(), value.strip()


def parse_yaml_value(value: str) -> Any:
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [parse_yaml_value(part.strip()) for part in inner.split(",")]
    return value.strip('"').strip("'")


def quote_yaml(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def normalize_label(text: str) -> str:
    cleaned = " ".join((text or "").strip().split())
    if not cleaned:
        return ""
    if cleaned.islower():
        return cleaned[:1].upper() + cleaned[1:]
    return cleaned


def default_query_for_topic(label: str) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for phrase in [label.lower(), *BASELINE_QUERY_TERMS]:
        normalized = " ".join(phrase.split())
        for token in normalized.split():
            if token in seen:
                continue
            seen.add(token)
            parts.append(token)
    return " ".join(parts)


def unique_topic_id(label: str, existing_topics: list[TopicEntry]) -> str:
    base = slug_for_topic_id(label)
    existing = {topic.id for topic in existing_topics}
    if base not in existing:
        return base
    index = 2
    while f"{base}-{index}" in existing:
        index += 1
    return f"{base}-{index}"


def slug_for_topic_id(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "topic"


def normalize_topic_match_value(value: str) -> str:
    return " ".join((value or "").strip().casefold().split())


def normalize_sources(sources: list[str]) -> list[str]:
    valid = [source for source in SCOUT_SOURCES if source in set(sources)]
    if not valid:
        raise ValueError("At least one valid source is required")
    return valid


def normalize_cadence(cadence: str) -> str:
    if cadence not in CADENCES:
        raise ValueError(f"Invalid cadence: {cadence}")
    return cadence


def normalize_priority(priority: str) -> str:
    if priority not in PRIORITIES:
        raise ValueError(f"Invalid priority: {priority}")
    return priority


def source_display_name(source: str) -> str:
    labels = {
        "arxiv": "arXiv",
        "semantic_scholar": "Semantic Scholar",
        "openalex": "OpenAlex",
    }
    return labels.get(source, source.replace("_", " ").title())


def schedule_label(source: str) -> str:
    if source == "arxiv":
        return "Daily default"
    if source == "openalex":
        return "Rotating source job"
    if source == "semantic_scholar":
        return "Source cron/manual job"
    return "Configured source"


def schedule_description(source: str) -> str:
    if source == "arxiv":
        return "Used by the default nightly Scout pipeline when no explicit --topic override is supplied."
    if source == "openalex":
        return "Used by scripts/openalex_pipeline.sh unless PAPER_AGENT_OPENALEX_TOPIC overrides it."
    if source == "semantic_scholar":
        return "Used by Semantic Scholar scheduled/manual runs when no explicit --topic override is supplied."
    return "Used by matching source runs when no explicit --topic override is supplied."


def source_notes(source: str) -> str:
    if source == "openalex":
        return "OpenAlex selects from enabled daily or weekly topics for its rotating job."
    return "Explicit CLI --topic values override this config for that run."
