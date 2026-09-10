"""Infrastructure settings shared by the packaged application and existing CLI."""
from __future__ import annotations

import os


def ollama_url() -> str:
    value = os.environ.get("PAPER_AGENT_OLLAMA_URL", "http://localhost:11434").rstrip("/")
    return value if value.endswith("/api/generate") else value + "/api/generate"


def packaged() -> bool:
    return os.environ.get("PAPER_AGENT_PACKAGED", "0") == "1"


def default_sources() -> list[str]:
    return ["arxiv"] if packaged() else ["arxiv", "semantic_scholar", "openalex"]


def gemini_enabled() -> bool:
    # Preserve the existing native deployment unless explicitly configured.
    return os.environ.get("PAPER_AGENT_GEMINI_ENABLED", "0" if packaged() else "1") == "1"
