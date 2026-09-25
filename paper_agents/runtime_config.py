"""Infrastructure settings shared by the packaged application and existing CLI."""
from __future__ import annotations

import os


def ollama_url() -> str:
    value = os.environ.get("PAPER_AGENT_OLLAMA_URL", "http://localhost:11434").rstrip("/")
    return value if value.endswith("/api/generate") else value + "/api/generate"


def packaged() -> bool:
    return os.environ.get("PAPER_AGENT_PACKAGED", "0") == "1"


def default_sources() -> list[str]:
    settings = migration_settings()
    if settings is not None:
        return settings["sources"]
    return ["arxiv"] if packaged() else ["arxiv", "semantic_scholar", "openalex"]


def gemini_enabled() -> bool:
    settings = migration_settings()
    if settings is not None:
        return settings["PAPER_AGENT_GEMINI_ENABLED"]
    # Preserve the existing native deployment unless explicitly configured.
    return os.environ.get("PAPER_AGENT_GEMINI_ENABLED", "0" if packaged() else "1") == "1"


def migration_settings() -> dict | None:
    """Explicit opt-in parity settings; never infer native configuration."""
    mode = os.environ.get("PAPER_AGENT_STARTUP_MODE", "fresh")
    if mode not in {"fresh", "imported"}:
        raise ValueError("Invalid PAPER_AGENT_STARTUP_MODE")
    if mode == "fresh":
        return None
    if not packaged():
        raise ValueError("Imported startup requires packaged mode")
    sources = os.environ.get("PAPER_AGENT_DEFAULT_SOURCES", "").split(",")
    if not sources or len(set(sources)) != len(sources) or not set(sources) <= {"arxiv", "openalex", "semantic_scholar"}:
        raise ValueError("Explicit valid migration default sources required")
    flags = {}
    for name in ("PAPER_AGENT_GEMINI_ENABLED", "PAPER_AGENT_TELEMETRY"):
        value = os.environ.get(name)
        if value not in {"0", "1"}:
            raise ValueError("Explicit 0/1 migration integration flags required")
        flags[name] = value == "1"
    if os.environ.get("PAPER_MODEL_MODE") != "verify":
        raise ValueError("Migration requires verify-only model mode")
    if os.environ.get("PAPER_AGENT_MODEL") != "qwen2.5:1.5b-instruct" or os.environ.get("PAPER_AGENT_EXPECTED_MODEL_DIGEST") != "65ec06548149b04c096a120e4a6da9d4017ea809c91734ea5631e89f96ddc57b":
        raise ValueError("Migration requires inventoried Qwen identity")
    if not os.environ.get("PAPER_AGENT_IMPORT_MANIFEST"):
        raise ValueError("Explicit import manifest required")
    return {"sources":sources, **flags}
