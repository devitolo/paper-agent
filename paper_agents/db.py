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


def db_stats(db_path: Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    init_db(db_path)
    with connect_db(db_path) as connection:
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ["papers", "scout_runs", "scout_candidates", "artifacts", "feedback"]
        }
        selected_candidates = connection.execute(
            "SELECT COUNT(*) FROM scout_candidates WHERE selected = 1"
        ).fetchone()[0]
        artifact_types = [
            {"artifact_type": row[0], "count": row[1]}
            for row in connection.execute(
                """
                SELECT artifact_type, COUNT(*)
                FROM artifacts
                GROUP BY artifact_type
                ORDER BY artifact_type
                """
            )
        ]
        latest_run = connection.execute(
            """
            SELECT id, started_at, source, fetch_limit, keep_limit, mode
            FROM scout_runs
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

    return {
        "db_path": str(db_path),
        "counts": counts,
        "selected_candidates": selected_candidates,
        "artifact_types": artifact_types,
        "latest_run": row_to_dict(
            latest_run,
            ["id", "started_at", "source", "fetch_limit", "keep_limit", "mode"],
        ),
    }


def recent_runs(db_path: Path = DEFAULT_DB_PATH, limit: int = 10) -> dict[str, Any]:
    init_db(db_path)
    limit = max(1, limit)
    with connect_db(db_path) as connection:
        rows = connection.execute(
            """
            SELECT
                scout_runs.id,
                scout_runs.started_at,
                scout_runs.source,
                scout_runs.fetch_limit,
                scout_runs.keep_limit,
                scout_runs.mode,
                scout_runs.topics_json,
                COUNT(scout_candidates.id) AS candidate_count,
                SUM(CASE WHEN scout_candidates.selected = 1 THEN 1 ELSE 0 END) AS selected_count
            FROM scout_runs
            LEFT JOIN scout_candidates ON scout_candidates.run_id = scout_runs.id
            GROUP BY scout_runs.id
            ORDER BY scout_runs.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    runs = []
    for row in rows:
        runs.append(
            {
                "id": row[0],
                "started_at": row[1],
                "source": row[2],
                "fetch_limit": row[3],
                "keep_limit": row[4],
                "mode": row[5],
                "topics": decode_json(row[6], []),
                "candidate_count": row[7],
                "selected_count": row[8] or 0,
            }
        )
    return {"db_path": str(db_path), "runs": runs}


def list_papers(
    db_path: Path = DEFAULT_DB_PATH,
    limit: int = 20,
    selected_only: bool = False,
) -> dict[str, Any]:
    init_db(db_path)
    limit = max(1, limit)
    selected_clause = "WHERE latest.selected = 1" if selected_only else ""
    with connect_db(db_path) as connection:
        rows = connection.execute(
            f"""
            WITH latest AS (
                SELECT
                    scout_candidates.paper_id,
                    scout_candidates.score,
                    scout_candidates.selected,
                    scout_candidates.ranking_reason,
                    scout_candidates.matched_keywords_json,
                    ROW_NUMBER() OVER (
                        PARTITION BY scout_candidates.paper_id
                        ORDER BY scout_candidates.run_id DESC
                    ) AS row_number
                FROM scout_candidates
            ), artifact_counts AS (
                SELECT paper_id, COUNT(*) AS artifact_count
                FROM artifacts
                GROUP BY paper_id
            )
            SELECT
                papers.id,
                papers.source,
                papers.source_id,
                papers.title,
                papers.published,
                papers.url,
                latest.score,
                latest.selected,
                latest.ranking_reason,
                latest.matched_keywords_json,
                COALESCE(artifact_counts.artifact_count, 0) AS artifact_count
            FROM papers
            LEFT JOIN latest ON latest.paper_id = papers.id AND latest.row_number = 1
            LEFT JOIN artifact_counts ON artifact_counts.paper_id = papers.id
            {selected_clause}
            ORDER BY papers.last_seen_at DESC, papers.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    papers = []
    for row in rows:
        papers.append(
            {
                "id": row[0],
                "source": row[1],
                "source_id": row[2],
                "title": row[3],
                "published": row[4],
                "url": row[5],
                "score": row[6],
                "selected": bool(row[7]) if row[7] is not None else False,
                "ranking_reason": row[8],
                "matched_keywords": decode_json(row[9], []),
                "artifact_count": row[10],
            }
        )
    return {"db_path": str(db_path), "papers": papers}


def row_to_dict(
    row: sqlite3.Row | tuple[Any, ...] | None,
    columns: list[str],
) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(zip(columns, row))


def decode_json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback
