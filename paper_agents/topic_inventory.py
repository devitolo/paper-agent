from __future__ import annotations

from datetime import date
from typing import Any

from paper_agents.scout import DEFAULT_SCOUT_TOPICS


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


def openalex_rotating_topic(today: date | None = None) -> str:
    current = today or date.today()
    return OPENALEX_ROTATING_TOPICS[int(current.strftime("%j")) % len(OPENALEX_ROTATING_TOPICS)]


def scout_topic_inventory(today: date | None = None) -> list[dict[str, Any]]:
    active_openalex_topic = openalex_rotating_topic(today)
    return [
        {
            "source": "arxiv",
            "label": "arXiv",
            "schedule": "Daily default",
            "description": "Used by the default nightly Scout pipeline when no source or topic override is supplied.",
            "topics": list(DEFAULT_SCOUT_TOPICS),
            "active_topics": list(DEFAULT_SCOUT_TOPICS),
            "notes": "These defaults are also used by manual arXiv runs without --topic.",
        },
        {
            "source": "openalex",
            "label": "OpenAlex",
            "schedule": "Weekly rotating script",
            "description": "Used by scripts/openalex_pipeline.sh unless PAPER_AGENT_OPENALEX_TOPIC overrides it.",
            "topics": list(OPENALEX_ROTATING_TOPICS),
            "active_topics": [active_openalex_topic],
            "notes": "The active topic rotates by day of year; operator overrides are external to this read-only page.",
        },
        {
            "source": "semantic_scholar",
            "label": "Semantic Scholar",
            "schedule": "External/manual cron override",
            "description": "The source adapter falls back to the shared default Scout topics, but the Mini cron usually supplies an explicit topic.",
            "topics": list(SEMANTIC_SCHOLAR_CRON_TOPICS),
            "active_topics": list(SEMANTIC_SCHOLAR_CRON_TOPICS),
            "notes": "Actual cron edits live outside the repo; update this read model when the documented cron topic changes.",
        },
    ]
