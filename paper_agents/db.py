from __future__ import annotations

import json
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


def connect_db(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def record_scout_output(
    db_path: Path,
    scout_output: dict[str, Any],
    *,
    topics: list[str],
    fetch_limit: int,
    keep_limit: int,
    mode: str,
) -> dict[str, Any]:
    init_db(db_path)
    candidates = scout_output.get("candidates") or scout_output.get("selected", [])
    with connect_db(db_path) as connection:
        run_id = insert_scout_run(
            connection,
            source=scout_output.get("source") or "unknown",
            fetch_limit=fetch_limit,
            keep_limit=keep_limit,
            topics=topics,
            mode=mode,
        )
        paper_ids: dict[str, int] = {}
        for candidate in candidates:
            paper_id = upsert_paper(connection, candidate)
            paper_ids[candidate_registry_key(candidate)] = paper_id
            insert_scout_candidate(connection, run_id, paper_id, candidate)
            if candidate.get("pdf_path"):
                insert_artifact(
                    connection,
                    paper_id,
                    artifact_type="pdf",
                    path=Path(candidate["pdf_path"]),
                    metadata={"pdf_url": candidate.get("pdf_url")},
                )

    return {
        "db_path": str(db_path),
        "run_id": run_id,
        "paper_ids": paper_ids,
        "candidate_count": len(candidates),
    }


def insert_scout_run(
    connection: sqlite3.Connection,
    *,
    source: str,
    fetch_limit: int,
    keep_limit: int,
    topics: list[str],
    mode: str,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO scout_runs (source, fetch_limit, keep_limit, topics_json, mode)
        VALUES (?, ?, ?, ?, ?)
        """,
        (source, fetch_limit, keep_limit, json.dumps(topics, ensure_ascii=False), mode),
    )
    return int(cursor.lastrowid)


def upsert_paper(connection: sqlite3.Connection, candidate: dict[str, Any]) -> int:
    source = str(candidate.get("source") or "unknown")
    source_id = str(candidate.get("source_id") or candidate.get("url") or candidate.get("title") or "unknown")
    title = str(candidate.get("title") or source_id)
    authors = candidate.get("authors") or []
    categories = candidate.get("categories") or []

    connection.execute(
        """
        INSERT INTO papers (
            source, source_id, title, url, pdf_url, published, updated, authors_json, categories_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source, source_id) DO UPDATE SET
            title = excluded.title,
            url = excluded.url,
            pdf_url = excluded.pdf_url,
            published = excluded.published,
            updated = excluded.updated,
            authors_json = excluded.authors_json,
            categories_json = excluded.categories_json,
            last_seen_at = datetime('now')
        """,
        (
            source,
            source_id,
            title,
            candidate.get("url"),
            candidate.get("pdf_url"),
            candidate.get("published") or candidate.get("publication_date"),
            candidate.get("updated"),
            json.dumps(authors, ensure_ascii=False),
            json.dumps(categories, ensure_ascii=False),
        ),
    )
    row = connection.execute(
        "SELECT id FROM papers WHERE source = ? AND source_id = ?",
        (source, source_id),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Could not load paper registry id for {source}:{source_id}")
    return int(row[0])


def insert_scout_candidate(
    connection: sqlite3.Connection,
    run_id: int,
    paper_id: int,
    candidate: dict[str, Any],
) -> None:
    connection.execute(
        """
        INSERT INTO scout_candidates (
            run_id, paper_id, score, matched_keywords_json, ranking_reason, selected
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id, paper_id) DO UPDATE SET
            score = excluded.score,
            matched_keywords_json = excluded.matched_keywords_json,
            ranking_reason = excluded.ranking_reason,
            selected = excluded.selected
        """,
        (
            run_id,
            paper_id,
            float(candidate.get("score") or 0),
            json.dumps(candidate.get("matched_keywords") or [], ensure_ascii=False),
            candidate.get("ranking_reason"),
            1 if candidate.get("selected") else 0,
        ),
    )


def insert_artifact(
    connection: sqlite3.Connection,
    paper_id: int,
    *,
    artifact_type: str,
    path: Path,
    model: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO artifacts (paper_id, artifact_type, path, model, metadata_json)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(paper_id, artifact_type, path) DO UPDATE SET
            model = excluded.model,
            metadata_json = excluded.metadata_json,
            created_at = datetime('now')
        """,
        (
            paper_id,
            artifact_type,
            str(path),
            model,
            json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
        ),
    )


def candidate_registry_key(candidate: dict[str, Any]) -> str:
    source = str(candidate.get("source") or "unknown")
    source_id = str(candidate.get("source_id") or candidate.get("url") or candidate.get("title") or "unknown")
    return f"{source}:{source_id}"
