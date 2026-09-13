from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path("data/paper_agent.db")
DEFAULT_SCHEMA_PATH = Path("sql/schema.sql")
DEFAULT_BUSY_TIMEOUT_MS = 30_000

WORKFLOW_STATES = {
    "created",
    "scouting",
    "scout_complete",
    "curating",
    "rescout_requested",
    "recommendations_ready",
    "awaiting_manual_discussion",
    "raw_feedback_received",
    "feedback_parsed",
    "profile_updated",
    "complete",
    "failed",
}


def init_db(db_path: Path = DEFAULT_DB_PATH, schema_path: Path = DEFAULT_SCHEMA_PATH) -> dict[str, Any]:
    if not schema_path.exists():
        raise RuntimeError(f"Schema file does not exist: {schema_path}")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    schema_sql = schema_path.read_text(encoding="utf-8")
    with sqlite3.connect(db_path) as connection:
        connection.executescript(schema_sql)
        migrate_structured_feedback_score_to_real(connection)
        connection.execute("PRAGMA foreign_keys = ON")
        tables = list_tables(connection)

    return {"db_path": str(db_path), "schema_path": str(schema_path), "tables": tables}


def migrate_structured_feedback_score_to_real(connection: sqlite3.Connection) -> None:
    columns = connection.execute("PRAGMA table_info(structured_feedback)").fetchall()
    score_column = next((column for column in columns if column[1] == "score"), None)
    if score_column is None or str(score_column[2]).upper() == "REAL":
        return

    connection.execute("PRAGMA foreign_keys = OFF")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS structured_feedback_new (
            id INTEGER PRIMARY KEY,
            parse_attempt_id INTEGER NOT NULL UNIQUE REFERENCES feedback_parse_attempts(id) ON DELETE CASCADE,
            paper_id INTEGER REFERENCES papers(id) ON DELETE SET NULL,
            decision TEXT,
            score REAL,
            observations_json TEXT NOT NULL DEFAULT '[]',
            preference_signals_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            CHECK (score IS NULL OR (score >= 1 AND score <= 5))
        );

        INSERT INTO structured_feedback_new (
            id, parse_attempt_id, paper_id, decision, score,
            observations_json, preference_signals_json, created_at
        )
        SELECT
            id, parse_attempt_id, paper_id, decision, score,
            observations_json, preference_signals_json, created_at
        FROM structured_feedback;

        DROP TABLE structured_feedback;
        ALTER TABLE structured_feedback_new RENAME TO structured_feedback;
        CREATE INDEX IF NOT EXISTS idx_structured_feedback_paper ON structured_feedback (paper_id);
        CREATE INDEX IF NOT EXISTS idx_structured_feedback_decision ON structured_feedback (decision);
        """
    )
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    connection.execute("PRAGMA foreign_keys = ON")
    if violations:
        raise RuntimeError(f"structured_feedback score migration produced foreign-key violations: {violations}")


def reset_db(db_path: Path = DEFAULT_DB_PATH, schema_path: Path = DEFAULT_SCHEMA_PATH) -> dict[str, Any]:
    if db_path.exists():
        db_path.unlink()
    for suffix in ["-wal", "-shm"]:
        sidecar = Path(str(db_path) + suffix)
        if sidecar.exists():
            sidecar.unlink()
    return init_db(db_path, schema_path)


def connect_db(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, timeout=DEFAULT_BUSY_TIMEOUT_MS / 1000)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {DEFAULT_BUSY_TIMEOUT_MS}")
    return connection


def list_tables(connection: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def create_workflow_cycle(
    connection: sqlite3.Connection,
    *,
    mode: str,
    max_scout_attempts: int,
    metadata: dict[str, Any] | None = None,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO workflow_cycles (state, mode, max_scout_attempts, metadata_json)
        VALUES ('created', ?, ?, ?)
        """,
        (mode, max_scout_attempts, json_dumps(metadata or {})),
    )
    return int(cursor.lastrowid)


def update_workflow_state(connection: sqlite3.Connection, cycle_id: int, state: str) -> None:
    if state not in WORKFLOW_STATES:
        raise ValueError(f"Unknown workflow state: {state}")
    connection.execute(
        "UPDATE workflow_cycles SET state = ?, updated_at = datetime('now') WHERE id = ?",
        (state, cycle_id),
    )


def increment_scout_attempts(connection: sqlite3.Connection, cycle_id: int) -> None:
    connection.execute(
        """
        UPDATE workflow_cycles
        SET scout_attempts_used = scout_attempts_used + 1,
            updated_at = datetime('now')
        WHERE id = ?
        """,
        (cycle_id,),
    )


def get_workflow_cycle(connection: sqlite3.Connection, cycle_id: int) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT id, created_at, updated_at, state, mode, max_scout_attempts, scout_attempts_used, metadata_json
        FROM workflow_cycles
        WHERE id = ?
        """,
        (cycle_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "created_at": row[1],
        "updated_at": row[2],
        "state": row[3],
        "mode": row[4],
        "max_scout_attempts": row[5],
        "scout_attempts_used": row[6],
        "metadata": decode_json(row[7], {}),
    }


def current_profile_version(connection: sqlite3.Connection) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT id, version, created_at, profile_json, source_structured_feedback_id, change_summary, active
        FROM profile_versions
        WHERE active = 1
        ORDER BY version DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "version": row[1],
        "created_at": row[2],
        "profile": decode_json(row[3], {}),
        "source_structured_feedback_id": row[4],
        "change_summary": row[5],
        "active": bool(row[6]),
    }


def ensure_profile_version(
    connection: sqlite3.Connection,
    profile: dict[str, Any],
    *,
    change_summary: str = "Initial profile seed",
) -> int:
    current = current_profile_version(connection)
    if current:
        return int(current["id"])
    return create_profile_version(connection, profile, change_summary=change_summary)


def create_profile_version(
    connection: sqlite3.Connection,
    profile: dict[str, Any],
    *,
    source_structured_feedback_id: int | None = None,
    change_summary: str | None = None,
) -> int:
    row = connection.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM profile_versions").fetchone()
    version = int(row[0])
    connection.execute("UPDATE profile_versions SET active = 0 WHERE active = 1")
    cursor = connection.execute(
        """
        INSERT INTO profile_versions (
            version, profile_json, source_structured_feedback_id, change_summary, active
        )
        VALUES (?, ?, ?, ?, 1)
        """,
        (version, json_dumps(profile), source_structured_feedback_id, change_summary),
    )
    return int(cursor.lastrowid)


def active_scouting_guidance(connection: sqlite3.Connection) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT id, curator_run_id, guidance_text, created_at, expires_at, metadata_json
        FROM scouting_guidance
        WHERE active = 1 AND (expires_at IS NULL OR expires_at > datetime('now'))
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "curator_run_id": row[1],
        "guidance_text": row[2],
        "created_at": row[3],
        "expires_at": row[4],
        "metadata": decode_json(row[5], {}),
    }


def create_scouting_guidance(
    connection: sqlite3.Connection,
    *,
    curator_run_id: int | None,
    guidance_text: str,
    metadata: dict[str, Any] | None = None,
    active: bool = True,
) -> int:
    if active:
        connection.execute("UPDATE scouting_guidance SET active = 0 WHERE active = 1")
    cursor = connection.execute(
        """
        INSERT INTO scouting_guidance (curator_run_id, guidance_text, active, metadata_json)
        VALUES (?, ?, ?, ?)
        """,
        (curator_run_id, guidance_text, 1 if active else 0, json_dumps(metadata or {})),
    )
    return int(cursor.lastrowid)


def canonical_key_for_candidate(candidate: dict[str, Any]) -> str:
    arxiv_id = normalized_arxiv_id(candidate.get("arxiv_id"))
    if not arxiv_id and candidate.get("source") == "arxiv":
        arxiv_id = normalized_arxiv_id(candidate.get("source_id"))
    if arxiv_id:
        return f"arxiv:{arxiv_id}"
    doi = normalize_doi(candidate.get("doi"))
    if doi:
        return f"doi:{doi}"
    source = str(candidate.get("source") or "unknown")
    source_id = str(candidate.get("source_id") or candidate.get("url") or candidate.get("title") or "unknown")
    return f"{source}:{source_id}"


def upsert_paper(connection: sqlite3.Connection, candidate: dict[str, Any]) -> tuple[int, bool]:
    canonical_key = canonical_key_for_candidate(candidate)
    arxiv_id = normalized_arxiv_id(candidate.get("arxiv_id"))
    if not arxiv_id and candidate.get("source") == "arxiv":
        arxiv_id = normalized_arxiv_id(candidate.get("source_id"))
    doi = normalize_doi(candidate.get("doi"))
    title = str(candidate.get("title") or canonical_key)
    authors = candidate.get("authors") or []
    categories = candidate.get("categories") or []
    row = connection.execute("SELECT id FROM papers WHERE canonical_key = ?", (canonical_key,)).fetchone()
    is_new = row is None

    connection.execute(
        """
        INSERT INTO papers (
            canonical_key, title, abstract, published, updated, doi, arxiv_id, authors_json, categories_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(canonical_key) DO UPDATE SET
            title = excluded.title,
            abstract = COALESCE(excluded.abstract, papers.abstract),
            published = COALESCE(excluded.published, papers.published),
            updated = COALESCE(excluded.updated, papers.updated),
            doi = COALESCE(excluded.doi, papers.doi),
            arxiv_id = COALESCE(excluded.arxiv_id, papers.arxiv_id),
            authors_json = excluded.authors_json,
            categories_json = excluded.categories_json,
            last_discovered_at = datetime('now')
        """,
        (
            canonical_key,
            title,
            candidate.get("abstract"),
            candidate.get("published") or candidate.get("publication_date"),
            candidate.get("updated"),
            doi,
            arxiv_id,
            json_dumps(authors),
            json_dumps(categories),
        ),
    )
    paper_id = int(connection.execute("SELECT id FROM papers WHERE canonical_key = ?", (canonical_key,)).fetchone()[0])
    upsert_paper_source(connection, paper_id, candidate)
    return paper_id, is_new


def upsert_paper_source(connection: sqlite3.Connection, paper_id: int, candidate: dict[str, Any]) -> None:
    source = str(candidate.get("source") or "unknown")
    source_id = str(candidate.get("source_id") or candidate.get("url") or candidate.get("title") or "unknown")
    connection.execute(
        """
        INSERT INTO paper_sources (paper_id, source, source_id, url, pdf_url, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(source, source_id) DO UPDATE SET
            paper_id = excluded.paper_id,
            url = excluded.url,
            pdf_url = excluded.pdf_url,
            metadata_json = excluded.metadata_json
        """,
        (
            paper_id,
            source,
            source_id,
            candidate.get("url"),
            candidate.get("pdf_url"),
            json_dumps(candidate.get("metadata") or {}),
        ),
    )


def discovered_canonical_keys(db_path: Path = DEFAULT_DB_PATH) -> set[str]:
    init_db(db_path)
    with connect_db(db_path) as connection:
        rows = connection.execute("SELECT canonical_key FROM papers").fetchall()
    return {row[0] for row in rows if row[0]}


def seen_source_ids(db_path: Path = DEFAULT_DB_PATH, source: str | None = None) -> set[str]:
    init_db(db_path)
    with connect_db(db_path) as connection:
        if source:
            rows = connection.execute("SELECT source_id FROM paper_sources WHERE source = ?", (source,)).fetchall()
        else:
            rows = connection.execute("SELECT source_id FROM paper_sources").fetchall()
    return {row[0] for row in rows if row[0]}


def insert_scout_run(
    connection: sqlite3.Connection,
    *,
    workflow_cycle_id: int,
    attempt_number: int,
    source: str,
    target_candidates: int,
    max_candidates: int,
    freshness_months: int,
    topics: list[str],
    guidance_id: int | None,
    diagnostics: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    errors: list[str] | None = None,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO scout_runs (
            workflow_cycle_id, attempt_number, source, target_candidates, max_candidates,
            freshness_months, topics_json, guidance_id, diagnostics_json, warnings_json, errors_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            workflow_cycle_id,
            attempt_number,
            source,
            target_candidates,
            max_candidates,
            freshness_months,
            json_dumps(topics),
            guidance_id,
            json_dumps(diagnostics or {}),
            json_dumps(warnings or []),
            json_dumps(errors or []),
        ),
    )
    return int(cursor.lastrowid)


def complete_scout_run(
    connection: sqlite3.Connection,
    scout_run_id: int,
    *,
    diagnostics: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    errors: list[str] | None = None,
) -> None:
    connection.execute(
        """
        UPDATE scout_runs
        SET completed_at = datetime('now'),
            diagnostics_json = ?,
            warnings_json = ?,
            errors_json = ?
        WHERE id = ?
        """,
        (json_dumps(diagnostics or {}), json_dumps(warnings or []), json_dumps(errors or []), scout_run_id),
    )


def insert_scout_candidate(
    connection: sqlite3.Connection,
    *,
    scout_run_id: int,
    paper_id: int,
    retrieval_order: int,
    is_new: bool,
    excluded: bool = False,
    exclusion_reason: str | None = None,
    source_query: str | None = None,
    source_diagnostics: dict[str, Any] | None = None,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO scout_candidates (
            scout_run_id, paper_id, retrieval_order, is_new, excluded,
            exclusion_reason, source_query, source_diagnostics_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(scout_run_id, paper_id) DO UPDATE SET
            retrieval_order = excluded.retrieval_order,
            is_new = excluded.is_new,
            excluded = excluded.excluded,
            exclusion_reason = excluded.exclusion_reason,
            source_query = excluded.source_query,
            source_diagnostics_json = excluded.source_diagnostics_json
        """,
        (
            scout_run_id,
            paper_id,
            retrieval_order,
            1 if is_new else 0,
            1 if excluded else 0,
            exclusion_reason,
            source_query,
            json_dumps(source_diagnostics or {}),
        ),
    )
    if cursor.lastrowid:
        return int(cursor.lastrowid)
    row = connection.execute(
        "SELECT id FROM scout_candidates WHERE scout_run_id = ? AND paper_id = ?",
        (scout_run_id, paper_id),
    ).fetchone()
    return int(row[0])


def paper_has_scout_candidate_in_cycle(
    connection: sqlite3.Connection,
    *,
    paper_id: int,
    workflow_cycle_id: int,
    before_scout_run_id: int | None = None,
) -> bool:
    before_clause = "" if before_scout_run_id is None else "AND scout_runs.id < ?"
    params: tuple[Any, ...] = (
        (paper_id, workflow_cycle_id)
        if before_scout_run_id is None
        else (paper_id, workflow_cycle_id, before_scout_run_id)
    )
    row = connection.execute(
        f"""
        SELECT 1
        FROM scout_candidates
        JOIN scout_runs ON scout_runs.id = scout_candidates.scout_run_id
        WHERE scout_candidates.paper_id = ?
          AND scout_runs.workflow_cycle_id = ?
          {before_clause}
        LIMIT 1
        """,
        params,
    ).fetchone()
    return row is not None


def paper_has_prior_recommendation(
    connection: sqlite3.Connection,
    *,
    paper_id: int,
    before_scout_run_id: int | None = None,
) -> bool:
    before_clause = (
        ""
        if before_scout_run_id is None
        else "AND recommendations.created_at <= (SELECT started_at FROM scout_runs WHERE id = ?)"
    )
    params: tuple[Any, ...] = (paper_id,) if before_scout_run_id is None else (paper_id, before_scout_run_id)
    row = connection.execute(
        f"""
        SELECT 1
        FROM recommendations
        WHERE recommendations.paper_id = ?
          {before_clause}
        LIMIT 1
        """,
        params,
    ).fetchone()
    return row is not None


def recommended_ids_for_cycle(connection: sqlite3.Connection, workflow_cycle_id: int) -> set[int]:
    return {row[0] for row in connection.execute(
        "SELECT r.paper_id FROM recommendations r JOIN curator_runs cr ON cr.id = r.curator_run_id "
        "WHERE cr.workflow_cycle_id = ?", (workflow_cycle_id,))}


def paper_evidence_context(connection: sqlite3.Connection, paper_id: int) -> dict[str, Any]:
    rows = connection.execute(
        "SELECT id, artifact_type, path, metadata_json FROM artifacts WHERE paper_id = ? ORDER BY id DESC",
        (paper_id,),
    ).fetchall()
    pdf = any(row[1] == "pdf" for row in rows)
    triage = next((row for row in rows if row[1] == "triage_summary"), None)
    metadata = decode_json(triage[3], {}) if triage else {}
    abstract_only = bool(metadata.get("abstract_only") or metadata.get("source_type") == "source_abstract"
                         or metadata.get("full_text_available") is False)
    # A URL alone is not downloaded evidence. Legacy triage needs a PDF artifact
    # or explicit full-text provenance before receiving the full-text tier.
    return {"pdf_artifact": pdf, "triage_artifact_id": triage[0] if triage else None,
            "triage_path": triage[2] if triage else None,
            "abstract_only": abstract_only,
            "full_text_triage": bool(triage) and not abstract_only
                                and (pdf or metadata.get("full_text_available") is True)}


def eligible_candidates_for_cycle(connection: sqlite3.Connection, workflow_cycle_id: int) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT
            scout_candidates.id,
            scout_candidates.paper_id,
            papers.title,
            papers.abstract,
            papers.published,
            papers.canonical_key,
            papers.arxiv_id,
            paper_sources.source,
            paper_sources.source_id,
            paper_sources.url,
            paper_sources.pdf_url,
            scout_candidates.retrieval_order,
            scout_runs.id
        FROM scout_candidates
        JOIN scout_runs ON scout_runs.id = scout_candidates.scout_run_id
        JOIN papers ON papers.id = scout_candidates.paper_id
        LEFT JOIN paper_sources ON paper_sources.paper_id = papers.id
        WHERE scout_runs.workflow_cycle_id = ?
          AND scout_candidates.excluded = 0
          AND NOT EXISTS (
              SELECT 1 FROM recommendations r JOIN curator_runs cr ON cr.id = r.curator_run_id
              WHERE r.paper_id = papers.id AND cr.workflow_cycle_id = scout_runs.workflow_cycle_id
          )
          AND (paper_sources.id IS NULL OR paper_sources.id = (
              SELECT MIN(id) FROM paper_sources WHERE paper_id = papers.id
          ))
        ORDER BY scout_runs.attempt_number ASC, scout_candidates.retrieval_order ASC
        """,
        (workflow_cycle_id,),
    ).fetchall()
    return [
        {
            "scout_candidate_id": row[0],
            "paper_id": row[1],
            "title": row[2],
            "abstract": row[3],
            "published": row[4],
            "canonical_key": row[5],
            "arxiv_id": row[6],
            "source": row[7],
            "source_id": row[8],
            "url": row[9],
            "pdf_url": row[10],
            "retrieval_order": row[11],
            "scout_run_id": row[12],
        }
        for row in rows
    ]


def create_curator_run(
    connection: sqlite3.Connection,
    *,
    workflow_cycle_id: int,
    profile_version_id: int | None,
    scout_attempt_count: int,
    max_scout_attempts: int,
    min_quality_score: float,
    max_recommendations: int,
    model: str | None,
    metadata: dict[str, Any] | None = None,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO curator_runs (
            workflow_cycle_id, profile_version_id, scout_attempt_count, max_scout_attempts,
            min_quality_score, max_recommendations, model, metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            workflow_cycle_id,
            profile_version_id,
            scout_attempt_count,
            max_scout_attempts,
            min_quality_score,
            max_recommendations,
            model,
            json_dumps(metadata or {}),
        ),
    )
    return int(cursor.lastrowid)


def insert_curator_evaluation(
    connection: sqlite3.Connection,
    *,
    curator_run_id: int,
    paper_id: int,
    scout_candidate_id: int | None,
    score: float,
    rationale: str,
    matched_signals: list[str],
    quality_threshold_met: bool,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO curator_evaluations (
            curator_run_id, paper_id, scout_candidate_id, score, rationale,
            matched_signals_json, quality_threshold_met
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(curator_run_id, paper_id) DO UPDATE SET
            scout_candidate_id = excluded.scout_candidate_id,
            score = excluded.score,
            rationale = excluded.rationale,
            matched_signals_json = excluded.matched_signals_json,
            quality_threshold_met = excluded.quality_threshold_met
        """,
        (
            curator_run_id,
            paper_id,
            scout_candidate_id,
            score,
            rationale,
            json_dumps(matched_signals),
            1 if quality_threshold_met else 0,
        ),
    )
    if cursor.lastrowid:
        return int(cursor.lastrowid)
    row = connection.execute(
        "SELECT id FROM curator_evaluations WHERE curator_run_id = ? AND paper_id = ?",
        (curator_run_id, paper_id),
    ).fetchone()
    return int(row[0])


def insert_recommendation(
    connection: sqlite3.Connection,
    *,
    curator_run_id: int,
    paper_id: int,
    recommendation_order: int,
    rationale: str,
    status: str = "recommended",
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO recommendations (curator_run_id, paper_id, recommendation_order, rationale, status)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(curator_run_id, paper_id) DO UPDATE SET
            recommendation_order = excluded.recommendation_order,
            rationale = excluded.rationale,
            status = excluded.status
        """,
        (curator_run_id, paper_id, recommendation_order, rationale, status),
    )
    if cursor.lastrowid:
        return int(cursor.lastrowid)
    row = connection.execute(
        "SELECT id FROM recommendations WHERE curator_run_id = ? AND paper_id = ?",
        (curator_run_id, paper_id),
    ).fetchone()
    return int(row[0])


def update_curator_rescout(
    connection: sqlite3.Connection,
    curator_run_id: int,
    *,
    requested: bool,
    reason: str | None,
) -> None:
    connection.execute(
        "UPDATE curator_runs SET requested_rescout = ?, rescout_reason = ? WHERE id = ?",
        (1 if requested else 0, reason, curator_run_id),
    )


def insert_artifact(
    connection: sqlite3.Connection,
    paper_id: int,
    *,
    artifact_type: str,
    path: Path,
    model: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO artifacts (paper_id, artifact_type, path, model, metadata_json)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(paper_id, artifact_type, path) DO UPDATE SET
            model = excluded.model,
            metadata_json = excluded.metadata_json,
            created_at = datetime('now')
        """,
        (paper_id, artifact_type, str(path), model, json_dumps(metadata or {})),
    )
    if cursor.lastrowid:
        return int(cursor.lastrowid)
    row = connection.execute(
        "SELECT id FROM artifacts WHERE paper_id = ? AND artifact_type = ? AND path = ?",
        (paper_id, artifact_type, str(path)),
    ).fetchone()
    return int(row[0])


def create_raw_feedback(
    connection: sqlite3.Connection,
    *,
    content: str,
    paper_id: int | None = None,
    recommendation_id: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> tuple[int, bool]:
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    row = connection.execute("SELECT id FROM raw_feedback WHERE content_hash = ?", (content_hash,)).fetchone()
    if row:
        return int(row[0]), False
    cursor = connection.execute(
        """
        INSERT INTO raw_feedback (paper_id, recommendation_id, content, content_hash, metadata_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        (paper_id, recommendation_id, content, content_hash, json_dumps(metadata or {})),
    )
    return int(cursor.lastrowid), True


def create_feedback_parse_attempt(
    connection: sqlite3.Connection,
    *,
    raw_feedback_id: int,
    parser_name: str,
    parser_version: str,
    model: str | None,
    status: str,
    output: dict[str, Any] | None = None,
    error: str | None = None,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO feedback_parse_attempts (
            raw_feedback_id, parser_name, parser_version, model, status, error, output_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (raw_feedback_id, parser_name, parser_version, model, status, error, json_dumps(output or {})),
    )
    return int(cursor.lastrowid)


def create_structured_feedback(
    connection: sqlite3.Connection,
    *,
    parse_attempt_id: int,
    paper_id: int | None,
    decision: str | None,
    score: float | None,
    observations: list[str],
    preference_signals: list[str],
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO structured_feedback (
            parse_attempt_id, paper_id, decision, score, observations_json, preference_signals_json
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (parse_attempt_id, paper_id, decision, score, json_dumps(observations), json_dumps(preference_signals)),
    )
    return int(cursor.lastrowid)


def unapplied_structured_feedback(connection: sqlite3.Connection, limit: int | None = None) -> list[dict[str, Any]]:
    limit_clause = "" if limit is None else "LIMIT ?"
    params: tuple[Any, ...] = () if limit is None else (max(1, limit),)
    rows = connection.execute(
        f"""
        SELECT
            structured_feedback.id,
            structured_feedback.paper_id,
            structured_feedback.decision,
            structured_feedback.score,
            structured_feedback.observations_json,
            structured_feedback.preference_signals_json,
            structured_feedback.created_at
        FROM structured_feedback
        LEFT JOIN feedback_profile_applications
          ON feedback_profile_applications.structured_feedback_id = structured_feedback.id
        WHERE feedback_profile_applications.id IS NULL
        ORDER BY structured_feedback.id ASC
        {limit_clause}
        """,
        params,
    ).fetchall()
    return [
        {
            "id": row[0],
            "paper_id": row[1],
            "decision": row[2],
            "score": row[3],
            "observations": decode_json(row[4], []),
            "preference_signals": decode_json(row[5], []),
            "created_at": row[6],
        }
        for row in rows
    ]


def structured_feedback_by_ids(connection: sqlite3.Connection, structured_feedback_ids: list[int]) -> list[dict[str, Any]]:
    if not structured_feedback_ids:
        return []
    placeholders = ",".join("?" for _ in structured_feedback_ids)
    rows = connection.execute(
        f"""
        SELECT
            id,
            paper_id,
            decision,
            score,
            observations_json,
            preference_signals_json,
            created_at
        FROM structured_feedback
        WHERE id IN ({placeholders})
        ORDER BY id ASC
        """,
        tuple(structured_feedback_ids),
    ).fetchall()
    return [
        {
            "id": row[0],
            "paper_id": row[1],
            "decision": row[2],
            "score": row[3],
            "observations": decode_json(row[4], []),
            "preference_signals": decode_json(row[5], []),
            "created_at": row[6],
        }
        for row in rows
    ]


def all_structured_feedback(connection: sqlite3.Connection, limit: int | None = None) -> list[dict[str, Any]]:
    limit_clause = "" if limit is None else "LIMIT ?"
    params: tuple[Any, ...] = () if limit is None else (max(1, limit),)
    rows = connection.execute(
        f"""
        SELECT
            id,
            paper_id,
            decision,
            score,
            observations_json,
            preference_signals_json,
            created_at
        FROM structured_feedback
        ORDER BY id ASC
        {limit_clause}
        """,
        params,
    ).fetchall()
    return [
        {
            "id": row[0],
            "paper_id": row[1],
            "decision": row[2],
            "score": row[3],
            "observations": decode_json(row[4], []),
            "preference_signals": decode_json(row[5], []),
            "created_at": row[6],
        }
        for row in rows
    ]


def recent_structured_feedback_with_papers(connection: sqlite3.Connection, limit: int = 12) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT
            structured_feedback.id,
            structured_feedback.paper_id,
            structured_feedback.decision,
            structured_feedback.score,
            structured_feedback.observations_json,
            structured_feedback.preference_signals_json,
            structured_feedback.created_at,
            papers.title,
            papers.abstract,
            papers.categories_json,
            paper_sources.source
        FROM structured_feedback
        LEFT JOIN papers ON papers.id = structured_feedback.paper_id
        LEFT JOIN paper_sources ON paper_sources.paper_id = papers.id
         AND paper_sources.id = (
             SELECT MIN(id) FROM paper_sources WHERE paper_sources.paper_id = papers.id
         )
        ORDER BY structured_feedback.id DESC
        LIMIT ?
        """,
        (max(1, limit),),
    ).fetchall()
    return [
        {
            "id": row[0],
            "paper_id": row[1],
            "decision": row[2],
            "score": row[3],
            "observations": decode_json(row[4], []),
            "preference_signals": decode_json(row[5], []),
            "created_at": row[6],
            "title": row[7],
            "abstract": row[8],
            "categories": decode_json(row[9], []),
            "source": row[10],
        }
        for row in rows
    ]


def create_feedback_profile_applications(
    connection: sqlite3.Connection,
    structured_feedback_ids: list[int],
    profile_version_id: int,
) -> list[int]:
    application_ids = []
    for structured_feedback_id in structured_feedback_ids:
        cursor = connection.execute(
            """
            INSERT INTO feedback_profile_applications (structured_feedback_id, profile_version_id)
            VALUES (?, ?)
            """,
            (structured_feedback_id, profile_version_id),
        )
        application_ids.append(int(cursor.lastrowid))
    return application_ids


def create_feedback_profile_apply_attempt(
    connection: sqlite3.Connection,
    *,
    provider: str,
    model: str | None,
    structured_feedback_ids: list[int],
    dry_run: bool,
    status: str,
    error: str | None = None,
    profile_version_id: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO feedback_profile_apply_attempts (
            provider, model, structured_feedback_ids_json, dry_run, status,
            error, profile_version_id, metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            provider,
            model,
            json_dumps(structured_feedback_ids),
            1 if dry_run else 0,
            status,
            error,
            profile_version_id,
            json_dumps(metadata or {}),
        ),
    )
    return int(cursor.lastrowid)


def cleanup_legacy_feedback_statuses(
    db_path: Path = DEFAULT_DB_PATH,
    *,
    dry_run: bool = True,
    statuses: tuple[str, ...] = ("interested", "read_later", "reviewed"),
) -> dict[str, Any]:
    """Remove obsolete lightweight status rows without touching V2 feedback tables."""
    init_db(db_path)
    placeholders = ",".join("?" for _ in statuses)
    with connect_db(db_path) as connection:
        rows = connection.execute(
            f"""
            SELECT status, COUNT(*)
            FROM feedback
            WHERE status IN ({placeholders})
            GROUP BY status
            ORDER BY status
            """,
            statuses,
        ).fetchall()
        counts = {row[0]: row[1] for row in rows}
        total = sum(counts.values())
        if not dry_run and total:
            connection.execute(f"DELETE FROM feedback WHERE status IN ({placeholders})", statuses)
    return {
        "db_path": str(db_path),
        "dry_run": dry_run,
        "statuses": list(statuses),
        "rows_matched": total,
        "rows_deleted": 0 if dry_run else total,
        "counts": counts,
        "preserved": ["not_interested", "raw_feedback", "structured_feedback"],
    }


def db_stats(db_path: Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    init_db(db_path)
    tracked = [
        "papers",
        "paper_sources",
        "workflow_cycles",
        "scout_runs",
        "scout_candidates",
        "curator_runs",
        "curator_evaluations",
        "recommendations",
        "artifacts",
        "raw_feedback",
        "feedback_parse_attempts",
        "structured_feedback",
        "feedback_profile_applications",
        "feedback_profile_apply_attempts",
        "profile_versions",
        "feedback",
    ]
    with connect_db(db_path) as connection:
        counts = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tracked}
        latest_cycle = connection.execute(
            """
            SELECT id, created_at, updated_at, state, mode, max_scout_attempts, scout_attempts_used
            FROM workflow_cycles
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    return {
        "db_path": str(db_path),
        "counts": counts,
        "latest_cycle": row_to_dict(
            latest_cycle,
            ["id", "created_at", "updated_at", "state", "mode", "max_scout_attempts", "scout_attempts_used"],
        ),
    }


def health_summary(db_path: Path = DEFAULT_DB_PATH, *, days: int = 21, source: str | None = None) -> dict[str, Any]:
    init_db(db_path)
    days = max(1, days)
    source_filter = None if source in (None, "", "all") else source
    window = f"-{days} days"
    with connect_db(db_path) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        available_sources = [
            row[0]
            for row in connection.execute(
                """
                SELECT source FROM (
                    SELECT DISTINCT source FROM scout_runs
                    UNION
                    SELECT DISTINCT source FROM paper_sources
                )
                WHERE source IS NOT NULL
                ORDER BY source
                """
            ).fetchall()
            if row[0]
        ]
        latest_cycle = row_to_dict(
            connection.execute(
                """
                SELECT id, created_at, updated_at, state, mode, max_scout_attempts, scout_attempts_used
                FROM workflow_cycles
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone(),
            ["id", "created_at", "updated_at", "state", "mode", "max_scout_attempts", "scout_attempts_used"],
        )
        latest_recommendation_day = _scalar(
            connection,
            """
            SELECT MAX(date(recommendations.created_at))
            FROM recommendations
            LEFT JOIN paper_sources ON paper_sources.paper_id = recommendations.paper_id
            WHERE (? IS NULL OR paper_sources.source = ?)
            """,
            (source_filter, source_filter),
        )
        papers_waiting = _scalar(
            connection,
            """
            WITH latest_recommendation AS (
                SELECT
                    paper_id,
                    ROW_NUMBER() OVER (PARTITION BY paper_id ORDER BY curator_run_id DESC, recommendation_order ASC) AS row_number
                FROM recommendations
            ), latest_feedback AS (
                SELECT
                    paper_id,
                    status,
                    ROW_NUMBER() OVER (PARTITION BY paper_id ORDER BY id DESC) AS row_number
                FROM feedback
            )
            SELECT COUNT(*)
            FROM latest_recommendation
            LEFT JOIN latest_feedback
              ON latest_feedback.paper_id = latest_recommendation.paper_id
             AND latest_feedback.row_number = 1
            WHERE latest_recommendation.row_number = 1
              AND latest_feedback.status IS NULL
            """,
        )
        feedback_profile = {
            "unapplied_structured_feedback_count": _scalar(
                connection,
                """
                SELECT COUNT(*)
                FROM structured_feedback
                LEFT JOIN feedback_profile_applications
                  ON feedback_profile_applications.structured_feedback_id = structured_feedback.id
                WHERE feedback_profile_applications.id IS NULL
                """,
            ),
            "recent_apply_failures_count": _scalar(
                connection,
                """
                SELECT COUNT(*)
                FROM feedback_profile_apply_attempts failed_attempts
                WHERE failed_attempts.status = 'failed'
                  AND failed_attempts.created_at >= datetime('now', '-7 days')
                  AND NOT EXISTS (
                    SELECT 1
                    FROM feedback_profile_apply_attempts successful_attempts
                    WHERE successful_attempts.dry_run = 0
                      AND successful_attempts.status = 'succeeded'
                      AND successful_attempts.id > failed_attempts.id
                  )
                """,
            ),
            "latest_profile": row_to_dict(
                connection.execute(
                    """
                    SELECT id, version, created_at, change_summary
                    FROM profile_versions
                    WHERE active = 1
                    ORDER BY version DESC
                    LIMIT 1
                    """
                ).fetchone(),
                ["id", "version", "created_at", "change_summary"],
            ),
        }
        daily = {
            "cycles": _rows(
                connection,
                """
                SELECT date(workflow_cycles.created_at) AS day, workflow_cycles.state, COUNT(*) AS cycle_count
                FROM workflow_cycles
                WHERE workflow_cycles.created_at >= datetime('now', ?)
                  AND (
                    ? IS NULL OR EXISTS (
                        SELECT 1 FROM scout_runs
                        WHERE scout_runs.workflow_cycle_id = workflow_cycles.id
                          AND scout_runs.source = ?
                    )
                  )
                GROUP BY day, workflow_cycles.state
                ORDER BY day DESC, workflow_cycles.state
                """,
                (window, source_filter, source_filter),
                ["day", "state", "cycle_count"],
            ),
            "scout": _rows(
                connection,
                """
                SELECT
                    date(scout_runs.started_at) AS day,
                    scout_runs.source,
                    COUNT(DISTINCT scout_runs.id) AS run_count,
                    COUNT(scout_candidates.id) AS candidate_count,
                    SUM(CASE WHEN scout_candidates.excluded = 0 THEN 1 ELSE 0 END) AS eligible_count,
                    SUM(CASE WHEN scout_candidates.excluded = 1 THEN 1 ELSE 0 END) AS excluded_count
                FROM scout_runs
                LEFT JOIN scout_candidates ON scout_candidates.scout_run_id = scout_runs.id
                WHERE scout_runs.started_at >= datetime('now', ?)
                  AND (? IS NULL OR scout_runs.source = ?)
                GROUP BY day, scout_runs.source
                ORDER BY day DESC, scout_runs.source
                """,
                (window, source_filter, source_filter),
                ["day", "source", "run_count", "candidate_count", "eligible_count", "excluded_count"],
            ),
            "curator": _rows(
                connection,
                """
                SELECT
                    date(curator_runs.created_at) AS day,
                    COALESCE(scout_runs.source, 'unknown') AS source,
                    COUNT(DISTINCT curator_evaluations.id) AS evaluation_count,
                    SUM(CASE WHEN curator_evaluations.quality_threshold_met = 1 THEN 1 ELSE 0 END) AS quality_met_count,
                    COUNT(DISTINCT recommendations.id) AS recommendation_count
                FROM curator_runs
                LEFT JOIN curator_evaluations ON curator_evaluations.curator_run_id = curator_runs.id
                LEFT JOIN scout_candidates ON scout_candidates.id = curator_evaluations.scout_candidate_id
                LEFT JOIN scout_runs ON scout_runs.id = scout_candidates.scout_run_id
                LEFT JOIN recommendations
                  ON recommendations.curator_run_id = curator_runs.id
                 AND recommendations.paper_id = curator_evaluations.paper_id
                WHERE curator_runs.created_at >= datetime('now', ?)
                  AND (? IS NULL OR COALESCE(scout_runs.source, 'unknown') = ?)
                GROUP BY day, source
                ORDER BY day DESC, source
                """,
                (window, source_filter, source_filter),
                ["day", "source", "evaluation_count", "quality_met_count", "recommendation_count"],
            ),
            "reviewer": _rows(
                connection,
                """
                WITH primary_source AS (
                    SELECT paper_id, source
                    FROM paper_sources
                    WHERE id IN (SELECT MIN(id) FROM paper_sources GROUP BY paper_id)
                )
                SELECT
                    date(recommendations.created_at) AS day,
                    COALESCE(primary_source.source, 'unknown') AS source,
                    COUNT(DISTINCT recommendations.id) AS recommendation_count,
                    COUNT(DISTINCT pdf.paper_id) AS pdf_count,
                    COUNT(DISTINCT triage.paper_id) AS triage_summary_count
                FROM recommendations
                LEFT JOIN primary_source ON primary_source.paper_id = recommendations.paper_id
                LEFT JOIN artifacts pdf
                  ON pdf.paper_id = recommendations.paper_id
                 AND pdf.artifact_type = 'pdf'
                LEFT JOIN artifacts triage
                  ON triage.paper_id = recommendations.paper_id
                 AND triage.artifact_type = 'triage_summary'
                WHERE recommendations.created_at >= datetime('now', ?)
                  AND (? IS NULL OR COALESCE(primary_source.source, 'unknown') = ?)
                GROUP BY day, source
                ORDER BY day DESC, source
                """,
                (window, source_filter, source_filter),
                ["day", "source", "recommendation_count", "pdf_count", "triage_summary_count"],
            ),
            "feedback": _rows(
                connection,
                """
                SELECT day, SUM(raw_feedback_count), SUM(structured_feedback_count), SUM(profile_version_count), SUM(apply_failure_count)
                FROM (
                    SELECT date(received_at) AS day, COUNT(*) AS raw_feedback_count, 0 AS structured_feedback_count, 0 AS profile_version_count, 0 AS apply_failure_count
                    FROM raw_feedback
                    WHERE received_at >= datetime('now', ?)
                    GROUP BY day
                    UNION ALL
                    SELECT date(created_at) AS day, 0, COUNT(*), 0, 0
                    FROM structured_feedback
                    WHERE created_at >= datetime('now', ?)
                    GROUP BY day
                    UNION ALL
                    SELECT date(created_at) AS day, 0, 0, COUNT(*), 0
                    FROM profile_versions
                    WHERE created_at >= datetime('now', ?)
                    GROUP BY day
                    UNION ALL
                    SELECT date(created_at) AS day, 0, 0, 0, COUNT(*)
                    FROM feedback_profile_apply_attempts
                    WHERE created_at >= datetime('now', ?)
                      AND status = 'failed'
                    GROUP BY day
                )
                GROUP BY day
                ORDER BY day DESC
                """,
                (window, window, window, window),
                [
                    "day",
                    "raw_feedback_count",
                    "structured_feedback_count",
                    "profile_version_count",
                    "apply_failure_count",
                ],
            ),
        }
        source_breakdown = {
            "funnel": _rows(
                connection,
                """
                SELECT
                    scout_runs.source,
                    COUNT(scout_candidates.id) AS candidate_count,
                    SUM(CASE WHEN scout_candidates.excluded = 0 THEN 1 ELSE 0 END) AS eligible_count,
                    SUM(CASE WHEN scout_candidates.excluded = 1 THEN 1 ELSE 0 END) AS excluded_count
                FROM scout_runs
                LEFT JOIN scout_candidates ON scout_candidates.scout_run_id = scout_runs.id
                WHERE scout_runs.started_at >= datetime('now', ?)
                  AND (? IS NULL OR scout_runs.source = ?)
                GROUP BY scout_runs.source
                ORDER BY candidate_count DESC, scout_runs.source
                """,
                (window, source_filter, source_filter),
                ["source", "candidate_count", "eligible_count", "excluded_count"],
            ),
            "recommendations": _rows(
                connection,
                """
                WITH primary_source AS (
                    SELECT paper_id, source
                    FROM paper_sources
                    WHERE id IN (SELECT MIN(id) FROM paper_sources GROUP BY paper_id)
                )
                SELECT COALESCE(primary_source.source, 'unknown') AS source, COUNT(DISTINCT recommendations.id) AS recommendation_count
                FROM recommendations
                LEFT JOIN primary_source ON primary_source.paper_id = recommendations.paper_id
                WHERE recommendations.created_at >= datetime('now', ?)
                  AND (? IS NULL OR COALESCE(primary_source.source, 'unknown') = ?)
                GROUP BY source
                ORDER BY recommendation_count DESC, source
                """,
                (window, source_filter, source_filter),
                ["source", "recommendation_count"],
            ),
            "exclusion_reasons": _rows(
                connection,
                """
                SELECT
                    scout_runs.source,
                    COALESCE(scout_candidates.exclusion_reason, 'unspecified') AS reason,
                    COUNT(*) AS count
                FROM scout_candidates
                JOIN scout_runs ON scout_runs.id = scout_candidates.scout_run_id
                WHERE scout_candidates.excluded = 1
                  AND scout_runs.started_at >= datetime('now', ?)
                  AND (? IS NULL OR scout_runs.source = ?)
                GROUP BY scout_runs.source, reason
                ORDER BY count DESC, scout_runs.source, reason
                LIMIT 20
                """,
                (window, source_filter, source_filter),
                ["source", "reason", "count"],
            ),
        }
        latest_scout = row_to_dict(
            connection.execute(
                """
                SELECT
                    scout_runs.id,
                    scout_runs.source,
                    scout_runs.started_at,
                    scout_runs.completed_at,
                    COUNT(scout_candidates.id) AS candidate_count,
                    SUM(CASE WHEN scout_candidates.excluded = 0 THEN 1 ELSE 0 END) AS eligible_count,
                    SUM(CASE WHEN scout_candidates.excluded = 1 THEN 1 ELSE 0 END) AS excluded_count
                FROM scout_runs
                LEFT JOIN scout_candidates ON scout_candidates.scout_run_id = scout_runs.id
                WHERE (? IS NULL OR scout_runs.source = ?)
                GROUP BY scout_runs.id
                ORDER BY scout_runs.id DESC
                LIMIT 1
                """,
                (source_filter, source_filter),
            ).fetchone(),
            ["id", "source", "started_at", "completed_at", "candidate_count", "eligible_count", "excluded_count"],
        )
        latest_scout_runs_by_source = _rows(
            connection,
            """
            WITH latest_runs AS (
                SELECT source, MAX(id) AS id
                FROM scout_runs
                WHERE (? IS NULL OR source = ?)
                GROUP BY source
            )
            SELECT
                scout_runs.id,
                scout_runs.source,
                scout_runs.started_at,
                scout_runs.completed_at,
                COUNT(scout_candidates.id) AS candidate_count,
                SUM(CASE WHEN scout_candidates.excluded = 0 THEN 1 ELSE 0 END) AS eligible_count,
                SUM(CASE WHEN scout_candidates.excluded = 1 THEN 1 ELSE 0 END) AS excluded_count,
                COUNT(DISTINCT recommendations.id) AS recommendation_count
            FROM latest_runs
            JOIN scout_runs ON scout_runs.id = latest_runs.id
            LEFT JOIN scout_candidates ON scout_candidates.scout_run_id = scout_runs.id
            LEFT JOIN curator_evaluations
              ON curator_evaluations.scout_candidate_id = scout_candidates.id
            LEFT JOIN recommendations
              ON recommendations.curator_run_id = curator_evaluations.curator_run_id
             AND recommendations.paper_id = curator_evaluations.paper_id
            GROUP BY scout_runs.id
            ORDER BY scout_runs.id DESC
            """,
            (source_filter, source_filter),
            [
                "id",
                "source",
                "started_at",
                "completed_at",
                "candidate_count",
                "eligible_count",
                "excluded_count",
                "recommendation_count",
            ],
        )
        recent_scout_runs = _rows(
            connection,
            """
            SELECT
                scout_runs.id,
                scout_runs.source,
                scout_runs.started_at,
                COUNT(scout_candidates.id) AS candidate_count,
                SUM(CASE WHEN scout_candidates.excluded = 0 THEN 1 ELSE 0 END) AS eligible_count,
                SUM(CASE WHEN scout_candidates.excluded = 1 THEN 1 ELSE 0 END) AS excluded_count
            FROM scout_runs
            LEFT JOIN scout_candidates ON scout_candidates.scout_run_id = scout_runs.id
            WHERE (? IS NULL OR scout_runs.source = ?)
              AND scout_runs.source IN ('arxiv', 'openalex', 'semantic_scholar')
              AND julianday(scout_runs.started_at) BETWEEN julianday('now', '-7 days') AND julianday('now')
            GROUP BY scout_runs.id
            ORDER BY scout_runs.id DESC
            LIMIT 3
            """,
            (source_filter, source_filter),
            ["id", "source", "started_at", "candidate_count", "eligible_count", "excluded_count"],
        )
        recent_cycle_recommendations = _rows(
            connection,
            """
            WITH primary_source AS (
                SELECT paper_id, source
                FROM paper_sources
                WHERE id IN (SELECT MIN(id) FROM paper_sources GROUP BY paper_id)
            )
            SELECT
                workflow_cycles.id,
                workflow_cycles.created_at,
                workflow_cycles.state,
                COUNT(DISTINCT recommendations.id) AS recommendation_count
            FROM workflow_cycles
            LEFT JOIN curator_runs ON curator_runs.workflow_cycle_id = workflow_cycles.id
            LEFT JOIN recommendations ON recommendations.curator_run_id = curator_runs.id
            LEFT JOIN primary_source ON primary_source.paper_id = recommendations.paper_id
            WHERE (
                ? IS NULL OR EXISTS (
                    SELECT 1 FROM scout_runs
                    WHERE scout_runs.workflow_cycle_id = workflow_cycles.id
                      AND scout_runs.source = ?
                )
            )
            GROUP BY workflow_cycles.id
            ORDER BY workflow_cycles.id DESC
            LIMIT 2
            """,
            (source_filter, source_filter),
            ["id", "created_at", "state", "recommendation_count"],
        )
        artifact_health = _artifact_health(connection, latest_cycle["id"] if latest_cycle else None, source_filter)
    connection.close()

    days_since_last_cycle = _days_since(latest_cycle["created_at"] if latest_cycle else None)
    days_since_last_recommendation = _days_since(latest_recommendation_day)
    top = {
        "latest_run_time": latest_cycle["created_at"] if latest_cycle else None,
        "latest_run_age_hours": None if days_since_last_cycle is None else round(days_since_last_cycle * 24, 1),
        "latest_workflow_state": latest_cycle["state"] if latest_cycle else None,
        "last_successful_recommendation_day": latest_recommendation_day,
        "papers_waiting_in_queue": papers_waiting,
        "unapplied_structured_feedback_count": feedback_profile["unapplied_structured_feedback_count"],
        "recent_profile_apply_failures_count": feedback_profile["recent_apply_failures_count"],
        "latest_zero_eligible_source": latest_scout["source"] if latest_scout and int(latest_scout["eligible_count"] or 0) == 0 else None,
    }
    summary = {
        "db": {
            "path": str(db_path),
            "size_bytes": db_path.stat().st_size if db_path.exists() else 0,
            "integrity": integrity,
        },
        "range": {"days": days, "source": source_filter or "all", "window": window},
        "available_sources": available_sources,
        "latest_cycle": latest_cycle,
        "latest_scout_run": latest_scout,
        "latest_scout_runs_by_source": latest_scout_runs_by_source,
        "recent_scout_runs": recent_scout_runs,
        "recent_cycle_recommendations": recent_cycle_recommendations,
        "latest_recommendation_day": latest_recommendation_day,
        "days_since_last_cycle": days_since_last_cycle,
        "days_since_last_recommendation": days_since_last_recommendation,
        "top": top,
        "daily": daily,
        "source_breakdown": source_breakdown,
        "artifact_health": artifact_health,
        "feedback_profile": feedback_profile,
    }
    summary["warnings"] = _health_warnings(summary)
    summary["profile_maintenance"] = _health_profile_maintenance(summary)
    return summary


def _artifact_health(connection: sqlite3.Connection, latest_cycle_id: int | None, source: str | None) -> dict[str, Any]:
    if latest_cycle_id is None:
        return {
            "latest_cycle_id": None,
            "recommendation_count": 0,
            "pdf_count": 0,
            "triage_summary_count": 0,
            "missing_pdf_count": 0,
            "missing_triage_summary_count": 0,
            "backfillable_missing_triage_summary_count": 0,
        }
    row = connection.execute(
        """
        WITH primary_source AS (
            SELECT paper_id, source, pdf_url
            FROM paper_sources
            WHERE id IN (SELECT MIN(id) FROM paper_sources GROUP BY paper_id)
        ), latest_recommendations AS (
            SELECT
                recommendations.id,
                recommendations.paper_id,
                primary_source.source,
                primary_source.pdf_url
            FROM recommendations
            JOIN curator_runs ON curator_runs.id = recommendations.curator_run_id
            LEFT JOIN primary_source ON primary_source.paper_id = recommendations.paper_id
            WHERE curator_runs.workflow_cycle_id = ?
              AND (? IS NULL OR COALESCE(primary_source.source, 'unknown') = ?)
        )
        SELECT
            COUNT(DISTINCT latest_recommendations.id) AS recommendation_count,
            COUNT(DISTINCT pdf.paper_id) AS pdf_count,
            COUNT(DISTINCT triage.paper_id) AS triage_summary_count,
            COUNT(
                DISTINCT CASE
                    WHEN triage.paper_id IS NULL
                     AND (
                        pdf.paper_id IS NOT NULL
                        OR (
                            COALESCE(latest_recommendations.pdf_url, '') != ''
                            AND (
                                latest_recommendations.source = 'arxiv'
                                OR lower(latest_recommendations.pdf_url) LIKE '%.pdf%'
                                OR lower(latest_recommendations.pdf_url) LIKE '%.pdf?%'
                            )
                        )
                     )
                    THEN latest_recommendations.id
                END
            ) AS backfillable_missing_triage_summary_count
        FROM latest_recommendations
        LEFT JOIN artifacts pdf
          ON pdf.paper_id = latest_recommendations.paper_id
         AND pdf.artifact_type = 'pdf'
        LEFT JOIN artifacts triage
          ON triage.paper_id = latest_recommendations.paper_id
         AND triage.artifact_type = 'triage_summary'
        """,
        (latest_cycle_id, source, source),
    ).fetchone()
    recommendation_count = int(row[0] or 0)
    pdf_count = int(row[1] or 0)
    triage_count = int(row[2] or 0)
    backfillable_missing_triage_count = int(row[3] or 0)
    return {
        "latest_cycle_id": latest_cycle_id,
        "recommendation_count": recommendation_count,
        "pdf_count": pdf_count,
        "triage_summary_count": triage_count,
        "missing_pdf_count": max(0, recommendation_count - pdf_count),
        "missing_triage_summary_count": max(0, recommendation_count - triage_count),
        "backfillable_missing_triage_summary_count": backfillable_missing_triage_count,
    }


def _health_warnings(summary: dict[str, Any]) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    integrity = summary["db"]["integrity"]
    if integrity != "ok":
        warnings.append({"level": "critical", "message": f"DB integrity check failed: {integrity}"})

    days_since_last_cycle = summary["days_since_last_cycle"]
    if days_since_last_cycle is None:
        warnings.append({"level": "warning", "message": "No workflow cycle has been recorded."})
    elif days_since_last_cycle * 24 > 72:
        warnings.append({"level": "critical", "message": "No workflow cycle has run in more than 72 hours."})
    elif days_since_last_cycle * 24 > 36:
        warnings.append({"level": "warning", "message": "No workflow cycle has run in more than 36 hours."})

    latest_cycle = summary["latest_cycle"]
    latest_cycle_age_hours = summary["top"]["latest_run_age_hours"]
    if latest_cycle and latest_cycle_age_hours is not None:
        terminal_states = {"recommendations_ready", "awaiting_manual_discussion", "profile_updated", "complete"}
        if latest_cycle["state"] != "failed" and (
            latest_cycle["state"] not in terminal_states and latest_cycle_age_hours > 6
        ):
            warnings.append(
                {
                    "level": "warning",
                    "message": f"Latest workflow cycle is {latest_cycle['state']} and {latest_cycle_age_hours:.1f} hours old.",
                }
            )

    for latest_scout in summary["latest_scout_runs_by_source"]:
        if not _is_recent_scheduled_scout(latest_scout):
            continue
        started_at = _format_local_run_time(latest_scout.get("started_at"))
        run_label = f"Scout run at {started_at}" if started_at else "Latest Scout run"
        candidate_count = int(latest_scout["candidate_count"] or 0)
        if candidate_count == 0:
            warnings.append(
                {
                    "level": "warning",
                    "scout_run_id": latest_scout["id"],
                    "message": (
                        f"{latest_scout['source']} {run_label} returned 0 candidates. "
                    ),
                }
            )
        elif candidate_count <= 2:
            warnings.append(
                {
                    "level": "warning",
                    "scout_run_id": latest_scout["id"],
                    "message": (
                        f"{latest_scout['source']} {run_label} returned only {candidate_count} candidates. "
                    ),
                }
            )
        if candidate_count > 0 and int(latest_scout["eligible_count"] or 0) == 0:
            warnings.append(
                {
                    "level": "warning",
                    "scout_run_id": latest_scout["id"],
                    "message": (
                        f"{latest_scout['source']} {run_label} had 0 eligible candidates. "
                    ),
                }
            )
        elif (
            0 < int(latest_scout["eligible_count"] or 0) < 10
            and int(latest_scout["recommendation_count"] or 0) == 0
        ):
            warnings.append(
                {
                    "level": "warning",
                    "scout_run_id": latest_scout["id"],
                    "message": (
                        f"{latest_scout['source']} {run_label} had only "
                        f"{latest_scout['eligible_count']} eligible candidates and produced 0 recommendations; "
                        "target pool: 10 eligible candidates. "
                    ),
                }
            )
        elif 0 < int(latest_scout["eligible_count"] or 0) < 10:
            warnings.append(
                {
                    "level": "warning",
                    "scout_run_id": latest_scout["id"],
                    "message": (
                        f"{latest_scout['source']} {run_label} had only "
                        f"{latest_scout['eligible_count']} eligible candidates; target pool: 10 eligible candidates. "
                    ),
                }
            )
        elif (
            int(latest_scout["eligible_count"] or 0) > 0
            and int(latest_scout["recommendation_count"] or 0) == 0
        ):
            warnings.append(
                {
                    "level": "warning",
                    "scout_run_id": latest_scout["id"],
                    "message": (
                        f"{latest_scout['source']} {run_label} had "
                        f"{latest_scout['eligible_count']} eligible candidates but produced 0 recommendations. "
                    ),
                }
            )

    low_ratio_runs = 0
    recent_scout_runs = [row for row in summary["recent_scout_runs"] if _is_recent_scheduled_scout(row)]
    for row in recent_scout_runs:
        candidates = int(row["candidate_count"] or 0)
        eligible = int(row["eligible_count"] or 0)
        if candidates > 0 and eligible / candidates < 0.10:
            low_ratio_runs += 1
    if len(recent_scout_runs) >= 3 and low_ratio_runs >= 3:
        warnings.append({"level": "warning", "message": "Eligible/stored ratio is below 10% for the latest 3 scheduled Scout runs in the last 7 days."})

    artifact_health = summary["artifact_health"]
    if artifact_health["backfillable_missing_triage_summary_count"] > 0:
        warnings.append(
            {
                "level": "warning",
                "message": (
                    f"Latest cycle has {artifact_health['backfillable_missing_triage_summary_count']} "
                    "recommended paper(s) with PDFs but without triage summaries."
                ),
            }
        )

    return warnings


def _is_recent_scheduled_scout(run: dict[str, Any]) -> bool:
    started_at = _parse_sqlite_datetime(run.get("started_at"))
    if started_at is None or run["source"] not in {"arxiv", "openalex", "semantic_scholar"}:
        return False
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - started_at
    return timedelta(0) <= age <= timedelta(days=7)


def _health_profile_maintenance(summary: dict[str, Any]) -> list[dict[str, str]]:
    warnings: list[dict[str, str]] = []
    feedback_profile = summary["feedback_profile"]
    if feedback_profile["recent_apply_failures_count"] > 0:
        warnings.append({"level": "warning", "message": "Gemini/profile apply failed in the last 7 days."})
    if feedback_profile["unapplied_structured_feedback_count"] > 0:
        warnings.append({"level": "warning", "message": "Structured feedback is waiting to be applied to the profile (current pending backlog, all dates)."})
    return warnings


def _rows(
    connection: sqlite3.Connection,
    sql: str,
    params: tuple[Any, ...] = (),
    columns: list[str] | None = None,
) -> list[dict[str, Any]]:
    rows = connection.execute(sql, params).fetchall()
    if columns is None:
        columns = [description[0] for description in connection.execute(sql, params).description]
    return [dict(zip(columns, row)) for row in rows]


def _scalar(connection: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> Any:
    row = connection.execute(sql, params).fetchone()
    return row[0] if row else None


def _days_since(value: str | None) -> float | None:
    parsed = _parse_sqlite_datetime(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return round((datetime.now(timezone.utc) - parsed).total_seconds() / 86400, 2)


def _format_local_run_time(value: str | None) -> str | None:
    parsed = _parse_sqlite_datetime(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone().strftime("%Y-%m-%d %-I:%M %p %Z")


def _parse_sqlite_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value)
    for candidate in (text, f"{text} 00:00:00"):
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            pass
    return None


def recent_runs(db_path: Path = DEFAULT_DB_PATH, limit: int = 10) -> dict[str, Any]:
    init_db(db_path)
    limit = max(1, limit)
    with connect_db(db_path) as connection:
        rows = connection.execute(
            """
            SELECT
                workflow_cycles.id,
                workflow_cycles.created_at,
                workflow_cycles.state,
                workflow_cycles.mode,
                workflow_cycles.scout_attempts_used,
                COUNT(DISTINCT scout_runs.id) AS scout_run_count,
                COUNT(DISTINCT curator_runs.id) AS curator_run_count,
                COUNT(DISTINCT recommendations.id) AS recommendation_count
            FROM workflow_cycles
            LEFT JOIN scout_runs ON scout_runs.workflow_cycle_id = workflow_cycles.id
            LEFT JOIN curator_runs ON curator_runs.workflow_cycle_id = workflow_cycles.id
            LEFT JOIN recommendations ON recommendations.curator_run_id = curator_runs.id
            GROUP BY workflow_cycles.id
            ORDER BY workflow_cycles.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return {
        "db_path": str(db_path),
        "runs": [
            {
                "id": row[0],
                "created_at": row[1],
                "state": row[2],
                "mode": row[3],
                "scout_attempts_used": row[4],
                "scout_run_count": row[5],
                "curator_run_count": row[6],
                "recommendation_count": row[7],
            }
            for row in rows
        ],
    }


def list_papers(db_path: Path = DEFAULT_DB_PATH, limit: int = 20, selected_only: bool = False) -> dict[str, Any]:
    init_db(db_path)
    limit = max(1, limit)
    selected_clause = "WHERE latest_recommendation.id IS NOT NULL" if selected_only else ""
    with connect_db(db_path) as connection:
        rows = connection.execute(
            f"""
            WITH latest_recommendation AS (
                SELECT
                    recommendations.id,
                    recommendations.paper_id,
                    recommendations.recommendation_order,
                    recommendations.rationale,
                    curator_evaluations.score,
                    ROW_NUMBER() OVER (
                        PARTITION BY recommendations.paper_id
                        ORDER BY recommendations.curator_run_id DESC, recommendations.recommendation_order ASC
                    ) AS row_number
                FROM recommendations
                LEFT JOIN curator_evaluations
                  ON curator_evaluations.curator_run_id = recommendations.curator_run_id
                 AND curator_evaluations.paper_id = recommendations.paper_id
            ), artifact_counts AS (
                SELECT paper_id, COUNT(*) AS artifact_count
                FROM artifacts
                GROUP BY paper_id
            ), primary_source AS (
                SELECT
                    paper_id,
                    source,
                    source_id,
                    url,
                    pdf_url,
                    ROW_NUMBER() OVER (PARTITION BY paper_id ORDER BY id ASC) AS row_number
                FROM paper_sources
            )
            SELECT
                papers.id,
                papers.canonical_key,
                papers.title,
                papers.published,
                primary_source.source,
                primary_source.source_id,
                primary_source.url,
                primary_source.pdf_url,
                latest_recommendation.score,
                latest_recommendation.recommendation_order,
                latest_recommendation.rationale,
                COALESCE(artifact_counts.artifact_count, 0) AS artifact_count
            FROM papers
            LEFT JOIN primary_source ON primary_source.paper_id = papers.id AND primary_source.row_number = 1
            LEFT JOIN latest_recommendation ON latest_recommendation.paper_id = papers.id AND latest_recommendation.row_number = 1
            LEFT JOIN artifact_counts ON artifact_counts.paper_id = papers.id
            {selected_clause}
            ORDER BY COALESCE(latest_recommendation.id, 0) DESC, papers.last_discovered_at DESC, papers.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return {
        "db_path": str(db_path),
        "papers": [
            {
                "id": row[0],
                "canonical_key": row[1],
                "title": row[2],
                "published": row[3],
                "source": row[4],
                "source_id": row[5],
                "url": row[6],
                "pdf_url": row[7],
                "score": row[8],
                "recommendation_order": row[9],
                "rationale": row[10],
                "artifact_count": row[11],
            }
            for row in rows
        ],
    }


def latest_recommendations(db_path: Path = DEFAULT_DB_PATH, limit: int = 50) -> list[dict[str, Any]]:
    init_db(db_path)
    with connect_db(db_path) as connection:
        row = connection.execute("SELECT MAX(id) FROM curator_runs").fetchone()
        if not row or row[0] is None:
            return []
        curator_run_id = int(row[0])
        rows = connection.execute(
            """
            SELECT
                recommendations.id,
                recommendations.recommendation_order,
                recommendations.rationale,
                recommendations.status,
                papers.id,
                papers.title,
                papers.published,
                papers.canonical_key,
                paper_sources.source,
                paper_sources.source_id,
                paper_sources.url,
                paper_sources.pdf_url,
                curator_evaluations.score,
                curator_evaluations.matched_signals_json
            FROM recommendations
            JOIN papers ON papers.id = recommendations.paper_id
            LEFT JOIN paper_sources ON paper_sources.paper_id = papers.id
            LEFT JOIN curator_evaluations
              ON curator_evaluations.curator_run_id = recommendations.curator_run_id
             AND curator_evaluations.paper_id = recommendations.paper_id
            WHERE recommendations.curator_run_id = ?
              AND (paper_sources.id IS NULL OR paper_sources.id = (
                  SELECT MIN(id) FROM paper_sources WHERE paper_id = papers.id
              ))
            ORDER BY recommendations.recommendation_order ASC
            LIMIT ?
            """,
            (curator_run_id, limit),
        ).fetchall()
    return [
        {
            "recommendation_id": row[0],
            "recommendation_order": row[1],
            "rationale": row[2],
            "status": row[3],
            "paper_id": row[4],
            "title": row[5],
            "published": row[6],
            "canonical_key": row[7],
            "source": row[8],
            "source_id": row[9],
            "url": row[10],
            "pdf_url": row[11],
            "score": row[12],
            "matched_signals": decode_json(row[13], []),
        }
        for row in rows
    ]


def row_to_dict(row: sqlite3.Row | tuple[Any, ...] | None, columns: list[str]) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(zip(columns, row))


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def decode_json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def normalized_arxiv_id(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text.removeprefix("arxiv:")
    return text


def normalize_doi(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    text = text.removeprefix("https://doi.org/").removeprefix("doi:")
    return text or None
