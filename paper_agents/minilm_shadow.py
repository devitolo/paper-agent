"""Isolated paired pre-Curator MiniLM shadow experiment."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from paper_agents import db
from paper_agents.curator_agent import DEFAULT_MIN_QUALITY_SCORE
from paper_agents.curator_scoring import SCORING_VERSION
from paper_agents.local_extract import DEFAULT_MODEL, DEFAULT_OLLAMA_URL, extract_paper
from paper_agents.minilm_eval import MODEL_ID, MODEL_REVISION, MODEL_SHA256, QUERY_VERSION, MiniLMScorer
from paper_agents.reviewer_agent import abstract_source_text


WOULD_SAMPLE = {"yes", "maybe", "no"}
REASON_TAGS = {
    "strong_fit", "practical_evidence", "too_theoretical", "weak_evidence",
    "duplicate_or_familiar", "unclear_summary", "off_topic",
}


def stable_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def load_frozen_pool(connection, source_run_id: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    run = connection.execute(
        "SELECT workflow_cycle_id, attempt_number, source, completed_at FROM scout_runs WHERE id = ?",
        (source_run_id,),
    ).fetchone()
    if run is None or run[3] is None:
        raise ValueError("Scout run is missing or incomplete")
    rows = connection.execute(
        """
        WITH primary_source AS (
            SELECT paper_id, source, source_id, url, pdf_url,
                   ROW_NUMBER() OVER (PARTITION BY paper_id ORDER BY id) AS row_number
            FROM paper_sources
        )
        SELECT sc.id, sc.paper_id, p.title, p.abstract, p.published, p.canonical_key,
               p.arxiv_id, ps.source, ps.source_id, ps.url, ps.pdf_url,
               sc.retrieval_order, sr.id, sr.attempt_number
        FROM scout_candidates sc
        JOIN scout_runs sr ON sr.id = sc.scout_run_id
        JOIN papers p ON p.id = sc.paper_id
        LEFT JOIN primary_source ps ON ps.paper_id = p.id AND ps.row_number = 1
        WHERE sr.workflow_cycle_id = ?
          AND sr.attempt_number <= ?
          AND sc.excluded = 0
          AND NOT EXISTS (
              SELECT 1 FROM recommendations r
              JOIN curator_runs cr ON cr.id = r.curator_run_id
              WHERE r.paper_id = sc.paper_id
                AND cr.workflow_cycle_id = sr.workflow_cycle_id
                AND cr.scout_attempt_count < ?
          )
        ORDER BY sr.attempt_number, sc.retrieval_order, sc.paper_id
        """,
        (run[0], run[1], run[1]),
    ).fetchall()
    seen: set[int] = set()
    candidates = []
    for row in rows:
        if int(row[1]) in seen:
            continue
        seen.add(int(row[1]))
        candidates.append({
            "scout_candidate_id": int(row[0]), "paper_id": int(row[1]),
            "title": str(row[2] or "Untitled paper"), "abstract": str(row[3] or ""),
            "published": row[4], "canonical_key": row[5], "arxiv_id": row[6],
            "source": row[7], "source_id": row[8], "url": row[9], "pdf_url": row[10],
            "retrieval_order": len(candidates) + 1, "scout_run_id": int(row[12]),
            "source_attempt": int(row[13]),
        })
    metadata = {
        "source_run_id": source_run_id, "workflow_cycle_id": int(run[0]),
        "attempt_number": int(run[1]), "source": str(run[2]), "completed_at": run[3],
    }
    return metadata, candidates


def load_persisted_curator_evaluations(
    connection, *, workflow_cycle_id: int, attempt_number: int,
) -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
    run = connection.execute(
        """SELECT id, profile_version_id, model, metadata_json
           FROM curator_runs
           WHERE workflow_cycle_id=? AND scout_attempt_count=?
           ORDER BY id DESC LIMIT 1""",
        (workflow_cycle_id, attempt_number),
    ).fetchone()
    if run is None:
        raise RuntimeError("Persisted Curator evaluation is unavailable for this Scout attempt")
    rows = connection.execute(
        """SELECT paper_id, score, rationale, matched_signals_json
           FROM curator_evaluations WHERE curator_run_id=?""",
        (run[0],),
    ).fetchall()
    evaluations = {
        int(row[0]): {
            "score": float(row[1]), "rationale": str(row[2]),
            "matched_signals": db.decode_json(row[3], []),
        }
        for row in rows
    }
    provenance = {
        "curator_run_id": int(run[0]), "profile_version_id": run[1], "model": run[2],
        "metadata_hash": stable_hash(db.decode_json(run[3], {})),
    }
    return evaluations, provenance


def generate_abstract_summary(candidate: dict[str, Any], *, model: str, ollama_url: str) -> dict[str, Any]:
    abstract = str(candidate.get("abstract") or "").strip()
    if not abstract:
        return {}
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "paper.abstract.txt"
        source.write_text(abstract_source_text(candidate, abstract), encoding="utf-8")
        extraction = extract_paper(
            source, model=model, ollama_url=ollama_url, max_chars=7000,
            limit_chunks=1, timeout=600, workers=1,
        )
    merged = extraction.get("merged") if isinstance(extraction, dict) else None
    return merged if isinstance(merged, dict) else {}


def load_existing_summary(connection, db_path: Path, paper_id: int) -> tuple[dict[str, Any], str] | None:
    row = connection.execute(
        """SELECT path, metadata_json FROM artifacts
           WHERE paper_id = ? AND artifact_type = 'triage_summary'
           ORDER BY id DESC LIMIT 1""",
        (paper_id,),
    ).fetchone()
    if row is None:
        return None
    path = Path(row[0])
    if not path.is_absolute():
        path = db_path.resolve().parent.parent / path
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        metadata = db.decode_json(row[1], {})
    except (OSError, ValueError, UnicodeError):
        return None
    merged = payload.get("merged") if isinstance(payload, dict) else None
    if not isinstance(merged, dict):
        return None
    abstract_only = bool(metadata.get("abstract_only") or metadata.get("source_type") == "source_abstract")
    return merged, "existing_abstract" if abstract_only else "existing_full_text"


def run_shadow_experiment(
    db_path: Path,
    *,
    source_run_id: int,
    input_limit: int = 10,
    output_limit: int = 3,
    min_quality_score: float = DEFAULT_MIN_QUALITY_SCORE,
    scorer: Callable[[str, str], float] | None = None,
    summarizer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    candidate_evaluator: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
    model: str = DEFAULT_MODEL,
    ollama_url: str = DEFAULT_OLLAMA_URL,
) -> dict[str, Any]:
    if input_limit < 1 or output_limit < 1 or output_limit > input_limit:
        raise ValueError("Require input_limit >= output_limit >= 1")
    db.init_db(db_path)
    with db.connect_db(db_path) as connection:
        existing = connection.execute(
            "SELECT id, status FROM minilm_shadow_runs WHERE source_run_id = ?", (source_run_id,),
        ).fetchone()
        if existing:
            return {"status": "already_exists", "shadow_run_id": int(existing[0]), "run_status": existing[1]}
        run_metadata, candidates = load_frozen_pool(connection, source_run_id)
        if candidate_evaluator is None:
            persisted_evaluations, curator_provenance = load_persisted_curator_evaluations(
                connection,
                workflow_cycle_id=run_metadata["workflow_cycle_id"],
                attempt_number=run_metadata["attempt_number"],
            )
        else:
            persisted_evaluations, curator_provenance = {}, {"mode": "injected_test_evaluator"}
        pool_snapshot = {
            **run_metadata,
            "papers": [{
                "paper_id": item["paper_id"], "scout_candidate_id": item["scout_candidate_id"],
                "retrieval_order": item["retrieval_order"], "source_run_id": item["scout_run_id"],
                "title_hash": hashlib.sha256(item["title"].encode()).hexdigest(),
                "abstract_hash": hashlib.sha256(item["abstract"].encode()).hexdigest(),
            } for item in candidates],
        }
        config = {
            "input_limit": input_limit, "output_limit": output_limit,
            "min_quality_score": min_quality_score, "scoring_version": SCORING_VERSION,
            "curator": curator_provenance, "summary_model": model,
            "minilm_revision": MODEL_REVISION,
        }
        cursor = connection.execute(
            """INSERT INTO minilm_shadow_runs (
                   source_run_id, source, status, pool_snapshot_json, pool_hash,
                   input_limit, output_limit, min_quality_score, minilm_model_id,
                   minilm_model_hash, minilm_query_version, config_hash
               ) VALUES (?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (source_run_id, run_metadata["source"], json.dumps(pool_snapshot, sort_keys=True),
             stable_hash(pool_snapshot), input_limit, output_limit, min_quality_score,
             MODEL_ID, MODEL_SHA256, QUERY_VERSION, stable_hash(config)),
        )
        shadow_run_id = int(cursor.lastrowid)

    started = time.monotonic()
    try:
        scorer = scorer or MiniLMScorer()
        for candidate in candidates:
            candidate["minilm_score"] = (
                float(scorer(candidate["title"], candidate["abstract"]))
                if candidate["title"].strip() and candidate["abstract"].strip() else None
            )
            if candidate["minilm_score"] is not None and not math.isfinite(candidate["minilm_score"]):
                raise RuntimeError("MiniLM returned a non-finite score")
        baseline = sorted(candidates, key=lambda item: (item["retrieval_order"], item["paper_id"]))[:input_limit]
        ranked = sorted(candidates, key=lambda item: (
            item["minilm_score"] is None,
            -(item["minilm_score"] if item["minilm_score"] is not None else -math.inf),
            item["retrieval_order"], item["paper_id"],
        ))
        for rank, candidate in enumerate(ranked, 1):
            candidate["minilm_rank"] = rank
        assisted = ranked[:input_limit]

        def evaluate_path(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            evaluated = []
            for position, candidate in enumerate(rows, 1):
                if candidate_evaluator is not None:
                    result = candidate_evaluator(candidate, {})
                else:
                    result = persisted_evaluations.get(candidate["paper_id"])
                    if result is None:
                        raise RuntimeError(
                            f"Persisted Curator evaluation missing for paper {candidate['paper_id']}"
                        )
                evaluated.append({**candidate, **result, "curator_input_position": position})
            evaluated.sort(key=lambda item: (item["score"], item.get("published") or ""), reverse=True)
            accepted = [item for item in evaluated if item["score"] >= min_quality_score][:output_limit]
            positions = {item["paper_id"]: index for index, item in enumerate(accepted, 1)}
            for item in evaluated:
                item["final_output_position"] = positions.get(item["paper_id"])
            return evaluated

        path_results = {"baseline": evaluate_path(baseline), "minilm": evaluate_path(assisted)}
        output_ids = {
            item["paper_id"] for rows in path_results.values() for item in rows
            if item["final_output_position"] is not None
        }
        summaries: dict[int, tuple[dict[str, Any], str]] = {}
        output_candidates = {
            paper_id: next(
                item for rows in path_results.values() for item in rows
                if item["paper_id"] == paper_id and item["final_output_position"] is not None
            )
            for paper_id in output_ids
        }
        with db.connect_db(db_path) as connection:
            for paper_id in output_ids:
                existing_summary = load_existing_summary(connection, db_path, paper_id)
                candidate = output_candidates[paper_id]
                if existing_summary:
                    summaries[paper_id] = existing_summary
                elif candidate["abstract"].strip():
                    generated = summarizer(candidate) if summarizer else generate_abstract_summary(
                        candidate, model=model, ollama_url=ollama_url,
                    )
                    summaries[paper_id] = (generated, "generated_abstract")
                else:
                    summaries[paper_id] = ({}, "unavailable")

            for path, rows in path_results.items():
                for item in rows:
                    connection.execute(
                        """INSERT INTO minilm_shadow_path_results (
                               shadow_run_id, paper_id, path, original_rank, minilm_raw_logit,
                               minilm_rank, curator_input_position, curator_score_value, curator_rationale,
                               curator_accepted, final_output_position
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (shadow_run_id, item["paper_id"], path, item["retrieval_order"],
                         item["minilm_score"], item.get("minilm_rank"), item["curator_input_position"],
                         item["score"], item["rationale"],
                         1 if item["final_output_position"] is not None else 0,
                         item["final_output_position"]),
                    )
            for paper_id in sorted(output_ids):
                candidate = output_candidates[paper_id]
                summary, summary_source = summaries[paper_id]
                positions = {
                    path: next((item["final_output_position"] for item in rows if item["paper_id"] == paper_id), None)
                    for path, rows in path_results.items()
                }
                connection.execute(
                    """INSERT INTO minilm_shadow_outputs (
                           shadow_run_id, paper_id, canonical_key_snapshot, source_snapshot,
                           source_id_snapshot, title_snapshot, abstract_snapshot,
                           problem_snapshot, why_it_matters_snapshot, approach_snapshot,
                           summary_source, summary_hash, baseline_output_position, minilm_output_position
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (shadow_run_id, paper_id, candidate.get("canonical_key"), candidate.get("source"),
                     candidate.get("source_id"), candidate["title"], candidate["abstract"],
                     summary.get("research_problem"), summary.get("why_it_matters"), summary.get("approach"),
                     summary_source, stable_hash(summary), positions["baseline"], positions["minilm"]),
                )
            connection.execute(
                "UPDATE minilm_shadow_runs SET status='complete', completed_at=datetime('now') WHERE id=?",
                (shadow_run_id,),
            )
        return {
            "status": "complete", "shadow_run_id": shadow_run_id, "source_run_id": source_run_id,
            "pool_count": len(candidates), "baseline_input_count": len(baseline),
            "minilm_input_count": len(assisted), "output_count": len(output_ids),
            "overlap_count": len({item["paper_id"] for item in baseline} & {item["paper_id"] for item in assisted}),
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
    except Exception as error:
        with db.connect_db(db_path) as connection:
            connection.execute(
                "UPDATE minilm_shadow_runs SET status='failed', failure_reason=?, completed_at=datetime('now') WHERE id=?",
                (f"{type(error).__name__}: {error}"[:1000], shadow_run_id),
            )
        raise


def save_shadow_decision(
    db_path: Path, *, output_id: int, would_sample: str, usefulness: int,
    reason_tags: list[str] | None = None,
) -> dict[str, Any]:
    if would_sample not in WOULD_SAMPLE:
        raise ValueError("Invalid would-sample decision")
    if usefulness not in range(1, 6):
        raise ValueError("Usefulness must be between 1 and 5")
    tags = sorted(set(reason_tags or []))
    if any(tag not in REASON_TAGS for tag in tags):
        raise ValueError("Invalid reason tag")
    db.init_db(db_path)
    with db.connect_db(db_path) as connection:
        row = connection.execute(
            "SELECT paper_id, canonical_key_snapshot FROM minilm_shadow_outputs WHERE id=?",
            (output_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Experiment output not found")
        paper_id, canonical_key = int(row[0]), str(row[1] or "").strip()
        if canonical_key:
            memberships = connection.execute(
                """SELECT DISTINCT shadow_run_id, paper_id FROM minilm_shadow_outputs
                   WHERE canonical_key_snapshot=?""",
                (canonical_key,),
            ).fetchall()
        else:
            memberships = connection.execute(
                """SELECT DISTINCT shadow_run_id, paper_id FROM minilm_shadow_outputs
                   WHERE paper_id=?""",
                (paper_id,),
            ).fetchall()
        for shadow_run_id, member_paper_id in memberships:
            connection.execute(
                """INSERT INTO minilm_shadow_decisions (
                       shadow_run_id, paper_id, would_sample, usefulness, reason_tags_json
                   ) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(shadow_run_id,paper_id) DO UPDATE SET
                       would_sample=excluded.would_sample, usefulness=excluded.usefulness,
                       reason_tags_json=excluded.reason_tags_json,
                       decided_at=datetime('now'), updated_at=datetime('now')""",
                (int(shadow_run_id), int(member_paper_id), would_sample, usefulness, json.dumps(tags)),
            )
    return {"output_id": output_id, "paper_id": paper_id, "would_sample": would_sample,
            "usefulness": usefulness, "updated_memberships": len(memberships)}


def load_shadow_review(db_path: Path, *, run_id: int | None = None) -> dict[str, Any]:
    """Load one globally deduplicated blind queue plus accumulated paired metrics."""
    db.init_db(db_path)
    with db.connect_db(db_path) as connection:
        run_rows = connection.execute(
            """SELECT r.id, r.source_run_id, r.source, r.status, r.started_at, r.completed_at,
                      COUNT(DISTINCT o.paper_id), COUNT(DISTINCT d.paper_id)
               FROM minilm_shadow_runs r
               LEFT JOIN minilm_shadow_outputs o ON o.shadow_run_id=r.id
               LEFT JOIN minilm_shadow_decisions d ON d.shadow_run_id=r.id
               GROUP BY r.id ORDER BY r.id DESC LIMIT 50"""
        ).fetchall()
        runs = [{
            "id": int(row[0]), "source_run_id": int(row[1]), "source": row[2], "status": row[3],
            "started_at": row[4], "completed_at": row[5], "paper_count": int(row[6]),
            "decided_count": int(row[7]),
        } for row in run_rows]
        rows = connection.execute(
            """SELECT o.id, o.paper_id, o.canonical_key_snapshot, o.title_snapshot,
                      o.abstract_snapshot, o.problem_snapshot, o.why_it_matters_snapshot,
                      o.approach_snapshot, d.id, d.would_sample, d.usefulness,
                      d.reason_tags_json, d.updated_at
               FROM minilm_shadow_outputs o
               JOIN minilm_shadow_runs r ON r.id=o.shadow_run_id AND r.status='complete'
               LEFT JOIN minilm_shadow_decisions d
                 ON d.shadow_run_id=o.shadow_run_id AND d.paper_id=o.paper_id
               ORDER BY o.id DESC"""
        ).fetchall()
        grouped_items: dict[str, dict[str, Any]] = {}
        decision_order: dict[str, tuple[str, int]] = {}
        for row in rows:
            identity = str(row[2] or f"paper:{int(row[1])}")
            if identity not in grouped_items:
                grouped_items[identity] = {
                    "id": int(row[0]), "paper_id": int(row[1]), "title": row[3],
                    "abstract": row[4], "problem": row[5], "why_it_matters": row[6],
                    "approach": row[7], "would_sample": None, "usefulness": None,
                    "reason_tags": [], "identity": identity,
                }
            if row[8] is not None:
                order = (str(row[12] or ""), int(row[8]))
                if order > decision_order.get(identity, ("", -1)):
                    decision_order[identity] = order
                    grouped_items[identity].update({
                        "would_sample": row[9], "usefulness": row[10],
                        "reason_tags": db.decode_json(row[11], []),
                    })
        items = list(grouped_items.values())
        items.sort(key=lambda item: (
            item["would_sample"] is not None,
            hashlib.sha256(f"minilm-shadow:{item['identity']}".encode()).digest(),
        ))

        metric_rows = connection.execute(
            """WITH ranked_decisions AS (
                   SELECT COALESCE(NULLIF(o.canonical_key_snapshot,''), 'paper:' || d.paper_id) identity,
                          d.would_sample, d.usefulness,
                          ROW_NUMBER() OVER (
                              PARTITION BY COALESCE(NULLIF(o.canonical_key_snapshot,''), 'paper:' || d.paper_id)
                              ORDER BY d.updated_at DESC, d.id DESC
                          ) decision_rank
                   FROM minilm_shadow_decisions d
                   JOIN minilm_shadow_outputs o
                     ON o.shadow_run_id=d.shadow_run_id AND o.paper_id=d.paper_id
               )
               SELECT r.source, p.path,
                      COUNT(DISTINCT CASE WHEN p.curator_accepted=1 THEN p.paper_id END) AS output_count,
                      COUNT(DISTINCT CASE WHEN p.curator_accepted=1 AND d.identity IS NOT NULL THEN p.paper_id END) AS reviewed_count,
                      COUNT(DISTINCT CASE WHEN p.curator_accepted=1 AND d.would_sample='yes' THEN p.paper_id END) AS yes_count,
                      COUNT(DISTINCT CASE WHEN p.curator_accepted=1 AND d.would_sample IN ('yes','maybe') THEN p.paper_id END) AS sample_count,
                      AVG(CASE WHEN p.curator_accepted=1 THEN d.usefulness END) AS mean_usefulness
               FROM minilm_shadow_runs r
               JOIN minilm_shadow_path_results p ON p.shadow_run_id=r.id
               JOIN minilm_shadow_outputs o
                 ON o.shadow_run_id=p.shadow_run_id AND o.paper_id=p.paper_id
               LEFT JOIN ranked_decisions d
                 ON d.identity=COALESCE(NULLIF(o.canonical_key_snapshot,''), 'paper:' || o.paper_id)
                AND d.decision_rank=1
               WHERE r.status='complete'
               GROUP BY r.source, p.path ORDER BY r.source, p.path"""
        ).fetchall()
        metrics = [{
            "source": row[0], "path": row[1], "output_count": int(row[2]),
            "reviewed_count": int(row[3]), "yes_count": int(row[4]), "sample_count": int(row[5]),
            "yes_rate": (float(row[4]) / row[3]) if row[3] else None,
            "yes_or_maybe_rate": (float(row[5]) / row[3]) if row[3] else None,
            "mean_usefulness": float(row[6]) if row[6] is not None else None,
        } for row in metric_rows]
        overlap = connection.execute(
            """SELECT COUNT(*) FROM minilm_shadow_outputs
               WHERE baseline_output_position IS NOT NULL AND minilm_output_position IS NOT NULL"""
        ).fetchone()[0]
        runtime = connection.execute(
            """SELECT COUNT(*), SUM(status='failed'),
                      AVG((julianday(completed_at)-julianday(started_at))*86400.0)
               FROM minilm_shadow_runs WHERE completed_at IS NOT NULL"""
        ).fetchone()
        runtime_rows = connection.execute(
            """SELECT source, COUNT(*), SUM(status='failed'),
                      AVG((julianday(completed_at)-julianday(started_at))*86400.0)
               FROM minilm_shadow_runs WHERE completed_at IS NOT NULL GROUP BY source ORDER BY source"""
        ).fetchall()
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for metric in metrics:
        grouped.setdefault(metric["source"], {})[metric["path"]] = metric
    comparisons = []
    for source, paths in sorted(grouped.items()):
        baseline_metric, minilm_metric = paths.get("baseline"), paths.get("minilm")
        if baseline_metric and minilm_metric:
            comparisons.append({
                "source": source,
                "yes_gain": minilm_metric["yes_count"] - baseline_metric["yes_count"],
                "sample_gain": minilm_metric["sample_count"] - baseline_metric["sample_count"],
            })
    return {
        "runs": runs, "selected_run": None, "items": items, "metrics": metrics,
        "comparisons": comparisons,
        "paper_count": len(items),
        "reviewed_count": sum(item["would_sample"] is not None for item in items),
        "overlap_count": int(overlap),
        "runtime": {"run_count": int(runtime[0]), "failed_count": int(runtime[1] or 0),
                    "mean_seconds": float(runtime[2]) if runtime[2] is not None else None},
        "runtime_by_source": [{
            "source": row[0], "run_count": int(row[1]), "failed_count": int(row[2] or 0),
            "mean_seconds": float(row[3]) if row[3] is not None else None,
        } for row in runtime_rows],
    }


def latest_unprocessed_source_run(db_path: Path, source: str) -> int | None:
    db.init_db(db_path)
    with db.connect_db(db_path) as connection:
        row = connection.execute(
            """SELECT sr.id FROM scout_runs sr
               LEFT JOIN minilm_shadow_runs shadow ON shadow.source_run_id=sr.id
               WHERE sr.source=? AND sr.completed_at IS NOT NULL AND shadow.id IS NULL
               ORDER BY sr.id DESC LIMIT 1""",
            (source,),
        ).fetchone()
    return int(row[0]) if row else None


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the isolated paired pre-Curator MiniLM shadow experiment")
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    parser.add_argument("--source", choices=("arxiv", "openalex", "semantic_scholar", "core"))
    parser.add_argument("--source-run-id", type=int)
    parser.add_argument("--input-limit", type=int, default=10)
    parser.add_argument("--output-limit", type=int, default=3)
    args = parser.parse_args()
    if (args.source is None) == (args.source_run_id is None):
        parser.error("provide exactly one of --source or --source-run-id")
    source_run_id = args.source_run_id or latest_unprocessed_source_run(args.db, args.source)
    if source_run_id is None:
        print(json.dumps({"status": "no_unprocessed_run", "source": args.source}, sort_keys=True))
        return
    result = run_shadow_experiment(
        args.db, source_run_id=source_run_id,
        input_limit=args.input_limit, output_limit=args.output_limit,
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
