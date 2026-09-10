"""Small Compose entry point and bounded readiness checks; no scheduled work."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from paper_agents import db
from paper_agents.local_extract import DEFAULT_MODEL
from paper_agents.runtime_config import ollama_url
from paper_agents.topics import load_topic_config

PACKAGE_SCHEMA_VERSION = 1
TEMPLATES = Path(__file__).with_name("package_templates")


def initialize(root: Path = Path("."), templates: Path = TEMPLATES) -> None:
    """Seed missing files only; reject broken existing state without reseeding."""
    for name in ("data", "config"):
        directory = root / name
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=directory):
            pass
    marker = root / "data/package-version.json"
    if marker.exists():
        version = json.loads(marker.read_text())
        if version.get("schema_version") != PACKAGE_SCHEMA_VERSION:
            raise RuntimeError("Unsupported package schema version; use the documented matching release/upgrade procedure")
    for relative, template in (("data/profile.json", "profile.json"), ("config/topics.yaml", "topics.yaml")):
        destination = root / relative
        if not destination.exists():
            with destination.open("x", encoding="utf-8") as output:
                output.write((templates / template).read_text(encoding="utf-8"))
    profile = json.loads((root / "data/profile.json").read_text())
    if not isinstance(profile, dict) or any(not isinstance(profile.get(key), list) for key in
            ("interests", "positive_signals", "negative_signals", "feedback_history")):
        raise RuntimeError("Invalid existing profile.json; restore or repair it explicitly")
    topics_path = root / "config/topics.yaml"
    if "topics:" not in [line.strip() for line in topics_path.read_text().splitlines()]:
        raise RuntimeError("Invalid existing topics.yaml: missing topics list; restore or repair it explicitly")
    load_topic_config(topics_path)
    db.init_db(root / "data/paper_agent.db", root / "sql/schema.sql")
    if not marker.exists():
        with marker.open("x", encoding="utf-8") as output:
            json.dump({"schema_version": PACKAGE_SCHEMA_VERSION,
                       "initialized_release": os.environ.get("PAPER_APP_IMAGE", "local-development")}, output)


def app_status(db_path: Path = db.DEFAULT_DB_PATH) -> dict:
    try:
        connection = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True, timeout=2)
        try:
            connection.execute("SELECT id FROM papers LIMIT 1").fetchall()
        finally:
            connection.close()
        return {"ready": True, "app": "ready", "database": "readable"}
    except sqlite3.Error:
        return {"ready": False, "app": "not_ready", "database": "unavailable; inspect app logs"}


def request_json(path: str, payload: dict | None = None, timeout: float = 5) -> dict:
    base = ollama_url().removesuffix("/api/generate")
    request = urllib.request.Request(base + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def model_status() -> dict:
    try:
        names = {item.get("name") for item in request_json("/api/tags").get("models", [])}
        present = DEFAULT_MODEL in names
        return {"api": "ready", "model": DEFAULT_MODEL, "present": present,
                "inference": "unchecked", "detail": "Run bounded inference check before discovery" if present
                else "Model not yet available; inspect prepare-model logs and retry preparation"}
    except Exception:
        return {"api": "unavailable", "model": DEFAULT_MODEL, "present": False,
                "inference": "unchecked", "detail": "Inspect ollama and prepare-model logs; retry preparation"}


def check_model(timeout: float = 120) -> dict:
    state = model_status()
    if not state["present"]:
        raise RuntimeError(state["detail"])
    result = request_json("/api/generate", {"model": DEFAULT_MODEL, "prompt": "Reply with the word READY.",
        "stream": False, "options": {"num_predict": 8}}, timeout=timeout)
    if not result.get("done") or not str(result.get("response", "")).strip():
        raise RuntimeError("Model inference did not complete successfully; inspect Ollama logs")
    return {**state, "inference": "ready", "checked_at": int(time.time())}


def main() -> int:
    try:
        command = sys.argv[1] if len(sys.argv) > 1 else "start"
        if command == "check-app":
            with urllib.request.urlopen("http://127.0.0.1:8000/ready", timeout=5) as response:
                return 0 if json.load(response)["ready"] else 1
        if command == "check-model":
            print(json.dumps(check_model(float(os.environ.get("PAPER_MODEL_CHECK_TIMEOUT", "120")))))
            return 0
        if command == "manual-scout":
            if len(sys.argv) != 4:
                raise RuntimeError("Expected manual-scout DB_PATH LOCK_FD")
            from paper_agents.manual_scout import worker_main
            return worker_main(Path(sys.argv[2]), int(sys.argv[3]))
        if command != "start":
            raise RuntimeError("Expected start, check-app, check-model or manual-scout")
        initialize()
        from paper_agents.web import run_review_ui
        run_review_ui(host="0.0.0.0", port=8000)
        return 0
    except Exception as error:
        print(f"Project Paper startup/readiness failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
