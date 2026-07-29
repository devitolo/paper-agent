from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path("data/paper_agent.db")
DEFAULT_SCHEMA_PATH = Path("sql/schema.sql")


def init_db(db_path: Path = DEFAULT_DB_PATH, schema_path: Path = DEFAULT_SCHEMA_PATH) -> dict[str, Any]:
    if not schema_path.exists():
        raise RuntimeError(f"Schema file does not exist: {schema_path}")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    schema_sql = schema_path.read_text(encoding="utf-8")
    with sqlite3.connect(db_path) as connection:
        connection.executescript(schema_sql)
        connection.execute("PRAGMA foreign_keys = ON")
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]

    return {
        "db_path": str(db_path),
        "schema_path": str(schema_path),
        "tables": tables,
    }
