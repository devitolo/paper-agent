"""Infrastructure settings shared by the packaged application and existing CLI."""
from __future__ import annotations

import os


def ollama_url() -> str:
    value = os.environ.get("PAPER_AGENT_OLLAMA_URL", "http://localhost:11434").rstrip("/")
    return value if value.endswith("/api/generate") else value + "/api/generate"


def packaged() -> bool:
    return os.environ.get("PAPER_AGENT_PACKAGED", "0") == "1"


def serving_only() -> bool:
    """Explicit model-free review UI; production defaults remain unchanged."""
    return os.environ.get("PAPER_AGENT_SERVING_ONLY", "0") == "1"


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
    if mode not in {"fresh", "imported", "serving", "serving-snapshot"}:
        raise ValueError("Invalid PAPER_AGENT_STARTUP_MODE")
    if mode in {"serving", "serving-snapshot"}:
        if not packaged() or not serving_only() or os.environ.get("PAPER_NON_PRODUCTION") != "1":
            raise ValueError("Serving bootstrap requires explicit packaged non-production mode")
        for flag in ("PAPER_AGENT_SCHEDULER_ENABLED", "PAPER_AGENT_GEMINI_ENABLED",
                     "PAPER_AGENT_TELEMETRY", "PAPER_AGENT_MINILM_ENABLED"):
            if os.environ.get(flag) != "0":
                raise ValueError("Synthetic serving bootstrap requires disabled " + flag)
        if os.environ.get("PAPER_AGENT_SYNC_SHARED_SECRET"):
            raise ValueError("Synthetic bootstrap forbids sync credentials")
        return None
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
