from __future__ import annotations

import json
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_INDUSTRY_CONFIG_PATH = Path("config/industry.json")


@dataclass
class ZenMLConfig:
    enabled: bool = True
    include_signals: list[str] = field(default_factory=lambda: [
        "internal assistant", "knowledge access", "shared ai platform", "employee-built",
        "code review", "testing", "migration", "maintenance", "developer workflow", "agent workflow",
    ])
    exclude_signals: list[str] = field(default_factory=lambda: [
        "promotional announcement", "generic tutorial", "root cause", "log analysis",
    ])
    max_items_per_run: int = 5
    schedule: str = "Sunday 11:30 PM America/Los_Angeles"


def load_zenml_config(path: Path = DEFAULT_INDUSTRY_CONFIG_PATH) -> ZenMLConfig:
    if not path.exists():
        return ZenMLConfig()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Invalid Industry / ZenML config {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError("Industry / ZenML config must be an object")
    include = _signals(value.get("include_signals"), "include_signals")
    exclude = _signals(value.get("exclude_signals"), "exclude_signals")
    maximum = value.get("max_items_per_run", 5)
    if not isinstance(maximum, int) or not 1 <= maximum <= 5:
        raise ValueError("max_items_per_run must be between 1 and 5")
    schedule = value.get("schedule", ZenMLConfig().schedule)
    if not isinstance(schedule, str) or not schedule.strip():
        raise ValueError("schedule must be non-empty text")
    return ZenMLConfig(
        enabled=bool(value.get("enabled", True)), include_signals=include,
        exclude_signals=exclude, max_items_per_run=maximum, schedule=schedule.strip(),
    )


def save_zenml_config(config: ZenMLConfig, path: Path = DEFAULT_INDUSTRY_CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(asdict(config), indent=2) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    temporary.replace(path)


def update_zenml_config(form: dict[str, list[str]], path: Path = DEFAULT_INDUSTRY_CONFIG_PATH) -> ZenMLConfig:
    maximum_text = form.get("max_items_per_run", ["5"])[0]
    try:
        maximum = int(maximum_text)
    except ValueError as error:
        raise ValueError("max_items_per_run must be a number") from error
    config = ZenMLConfig(
        enabled=form.get("enabled", [""])[0] == "1",
        include_signals=_split_lines(form.get("include_signals", [""])[0]),
        exclude_signals=_split_lines(form.get("exclude_signals", [""])[0]),
        max_items_per_run=maximum,
        schedule=ZenMLConfig().schedule,
    )
    if not 1 <= config.max_items_per_run <= 5:
        raise ValueError("max_items_per_run must be between 1 and 5")
    save_zenml_config(config, path)
    return config


def _signals(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be a list of strings")
    return [item.strip().lower() for item in value if item.strip()]


def _split_lines(value: str) -> list[str]:
    return [item.strip().lower() for line in value.splitlines() for item in line.split(",") if item.strip()]
