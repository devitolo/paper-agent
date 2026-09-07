from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

from paper_agents.scout import DEFAULT_SCOUT_TOPICS
from paper_agents.topics import DEFAULT_TOPIC_CONFIG_PATH, topic_inventory_from_config, select_topics_for_source


OPENALEX_ROTATING_TOPICS = [
    "AIOps observability incident response",
    "cloud operations anomaly detection remediation",
    "microservice diagnosis distributed systems debugging",
    "software reliability engineering production incidents",
    "LLM operations root cause analysis logs traces",
]

SEMANTIC_SCHOLAR_CRON_TOPICS = [
    "AIOps root cause analysis",
]


def openalex_rotating_topic(today: date | None = None, *, config_path: Path = DEFAULT_TOPIC_CONFIG_PATH) -> str:
    return select_topics_for_source(
        "openalex",
        today=today,
        cadences=("daily", "weekly"),
        path=config_path,
        fallback_topics=OPENALEX_ROTATING_TOPICS,
    )[0]


def scout_topic_inventory(
    today: date | None = None,
    *,
    now: datetime | None = None,
    config_path: Path = DEFAULT_TOPIC_CONFIG_PATH,
) -> list[dict[str, Any]]:
    try:
        return topic_inventory_from_config(config_path, today=today, now=now)
    except ValueError:
        active_openalex_topic = OPENALEX_ROTATING_TOPICS[int((today or date.today()).strftime("%j")) % len(OPENALEX_ROTATING_TOPICS)]
        return [
            {
                "source": "arxiv",
                "label": "arXiv",
                "schedule": "Daily default",
                "description": "Used by the default nightly Scout pipeline when no source or topic override is supplied.",
                "topics": list(DEFAULT_SCOUT_TOPICS),
                "active_topics": list(DEFAULT_SCOUT_TOPICS),
                "notes": "Fallback view from built-in defaults because config/topics.yaml could not be loaded.",
            },
            {
                "source": "openalex",
                "label": "OpenAlex",
                "schedule": "Weekly rotating script",
                "description": "Used by scripts/openalex_pipeline.sh unless PAPER_AGENT_OPENALEX_TOPIC overrides it.",
                "topics": list(OPENALEX_ROTATING_TOPICS),
                "active_topics": [active_openalex_topic],
                "notes": "Fallback view from built-in defaults because config/topics.yaml could not be loaded.",
            },
            {
                "source": "semantic_scholar",
                "label": "Semantic Scholar",
                "schedule": "External/manual cron override",
                "description": "Used by Semantic Scholar scheduled/manual runs when no topic override is supplied.",
                "topics": list(SEMANTIC_SCHOLAR_CRON_TOPICS),
                "active_topics": list(SEMANTIC_SCHOLAR_CRON_TOPICS),
                "notes": "Fallback view from built-in defaults because config/topics.yaml could not be loaded.",
            },
        ]
