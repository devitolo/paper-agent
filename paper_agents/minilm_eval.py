"""Temporary MiniLM ordering evaluation isolated from production ranking."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from pathlib import Path
from typing import Any, Callable

from paper_agents.db import DEFAULT_DB_PATH, connect_db, init_db


MODEL_ID = "cross-encoder/ms-marco-MiniLM-L6-v2"
MODEL_REVISION = "233902d25c440f23af6f7d6e94d2946bac0bee0a"
MODEL_SHA256 = "5d3e70fd0c9ff14b9b5169a51e957b7a9c74897afd0a35ce4bd318150c1d4d4a"
QUERY_VERSION = "project-paper-interests-max-v1"
MODEL_DIR = Path("/models/minilm")
DEFAULT_CANDIDATE_LIMIT = 50
INTERESTS = (
    "enterprise AI platform architecture governance organizational tradeoffs",
    "AIOps observability incident response diagnosis root cause analysis logs metrics traces system relationships",
    "agent reliability tool-agent coordination context failure handling recovery human oversight",
)
DECISIONS = {"send_to_curator", "maybe", "skip"}


def minilm_bucket(score: float | None) -> str:
    if score is None:
        return "Insufficient metadata"
    if score > -3:
        return "Prioritize"
    if score > -8:
        return "Lower priority"
    return "Low topical match"


class MiniLMScorer:
    """Pinned local cross-encoder. Imports remain outside the production web path."""

    def __init__(self, model_dir: Path = MODEL_DIR):
        model_path = model_dir / "onnx" / "model.onnx"
        if file_sha256(model_path) != MODEL_SHA256:
            raise RuntimeError("MiniLM model artifact does not match the approved digest")
        from transformers import AutoTokenizer
        import onnxruntime as ort

        self.tokenizer = AutoTokenizer.from_pretrained(
            model_dir,
            local_files_only=True,
            trust_remote_code=False,
            use_fast=True,
        )
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        self.session = ort.InferenceSession(
            str(model_path),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )
        self.input_names = [item.name for item in self.session.get_inputs()]
        if not {"input_ids", "attention_mask"}.issubset(self.input_names):
            raise RuntimeError("MiniLM runtime has unexpected inputs")

    def __call__(self, title: str, abstract: str) -> float:
        text = f"{title.strip()}\n{abstract.strip()}"
        scores = []
        for interest in INTERESTS:
            encoded = self.tokenizer(
                interest,
                text,
                truncation=True,
                max_length=512,
                padding=False,
                return_tensors="np",
            )
            result = self.session.run(None, {name: encoded[name] for name in self.input_names})
            score = float(result[0][0][0])
            if not math.isfinite(score):
                raise RuntimeError("MiniLM returned a non-finite score")
            scores.append(score)
        return max(scores)


def file_sha256(path: Path) -> str:
    sha = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                sha.update(block)
    except OSError as error:
        raise RuntimeError(f"MiniLM model artifact unavailable: {path}") from error
    return sha.hexdigest()


def create_minilm_eval(
    db_path: Path,
    *,
    source_run_id: int,
    scorer: Callable[[str, str], float] | None = None,
    candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
) -> dict[str, Any]:
    if candidate_limit < 1 or candidate_limit > 100:
        raise ValueError("candidate_limit must be between 1 and 100")
    init_db(db_path)
    with connect_db(db_path) as connection:
        existing = connection.execute(
            "SELECT id FROM minilm_eval_runs WHERE source_run_id = ?", (source_run_id,)
        ).fetchone()
        if existing:
            return {"status": "already_exists", "eval_run_id": int(existing[0]), "source_run_id": source_run_id}
        source_run = connection.execute(
            "SELECT source, workflow_cycle_id, completed_at FROM scout_runs WHERE id = ?",
            (source_run_id,),
        ).fetchone()
        if source_run is None or source_run[2] is None:
            raise ValueError("Scout run is missing or incomplete")
        candidates = load_eval_candidates(connection, source_run_id, db_path)

    scorer = scorer or MiniLMScorer()
    scored = []
    for candidate in candidates:
        score = None
        if candidate["title"].strip() and (candidate["abstract"] or "").strip():
            score = float(scorer(candidate["title"], candidate["abstract"]))
            if not math.isfinite(score):
                raise RuntimeError("MiniLM returned a non-finite score")
        scored.append({**candidate, "minilm_score": score, "minilm_bucket": minilm_bucket(score)})

    # Bound the experiment once, then rank the exact same papers in both modes.
    baseline = sorted(scored, key=lambda row: (row["retrieval_order"], row["paper_id"]))[:candidate_limit]
    assisted = sorted(
        baseline,
        key=lambda row: (
            row["minilm_score"] is None,
            -(row["minilm_score"] if row["minilm_score"] is not None else -math.inf),
            row["retrieval_order"],
            row["paper_id"],
        ),
    )
    metadata = {
        "source": source_run[0],
        "workflow_cycle_id": source_run[1],
        "source_completed_at": source_run[2],
        "eligible_candidate_count": len(scored),
        "evaluated_candidate_count": len(baseline),
        "candidate_limit": candidate_limit,
        "paper_ids": [row["paper_id"] for row in baseline],
        "interests": list(INTERESTS),
        "aggregation": "max",
        "score": "raw_single_logit",
        "buckets": {"prioritize": "> -3", "lower_priority": "-8 < score <= -3", "low_topical_match": "<= -8"},
        "unreviewed_semantics": "unknown",
    }
    with connect_db(db_path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO minilm_eval_runs (
                source_run_id, candidate_pool_snapshot_json, minilm_query_version, minilm_model_id
            ) VALUES (?, ?, ?, ?)
            """,
            (source_run_id, json.dumps(metadata, sort_keys=True), QUERY_VERSION, MODEL_ID),
        )
        eval_run_id = int(cursor.lastrowid)
        for mode, rows in (("baseline", baseline), ("minilm_assisted", assisted)):
            for rank, row in enumerate(rows, 1):
                connection.execute(
                    """
                    INSERT INTO minilm_eval_queue (
                        eval_run_id, paper_id, recommendation_mode, rank_position,
                        minilm_raw_logit, minilm_bucket, title_snapshot, abstract_snapshot,
                        problem_snapshot, why_it_matters_snapshot, approach_snapshot
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        eval_run_id, row["paper_id"], mode, rank,
                        row["minilm_score"], row["minilm_bucket"], row["title"], row["abstract"],
                        row["problem"], row["why_it_matters"], row["approach"],
                    ),
                )
    return {
        "status": "created",
        "eval_run_id": eval_run_id,
        "source_run_id": source_run_id,
        "candidate_count": len(scored),
        "queue_item_count": len(baseline) + len(assisted),
    }


def load_eval_candidates(connection: sqlite3.Connection, source_run_id: int, db_path: Path) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT sc.paper_id, sc.retrieval_order, p.title, p.abstract,
               triage.path
        FROM scout_candidates sc
        JOIN papers p ON p.id = sc.paper_id
        LEFT JOIN artifacts triage ON triage.id = (
            SELECT a.id FROM artifacts a
            WHERE a.paper_id = p.id AND a.artifact_type = 'triage_summary'
            ORDER BY a.id DESC LIMIT 1
        )
        WHERE sc.scout_run_id = ? AND sc.excluded = 0
        ORDER BY sc.retrieval_order, sc.paper_id
        """,
        (source_run_id,),
    ).fetchall()
    result = []
    for paper_id, order, title, abstract, summary_path in rows:
        summary = load_summary_snapshot(db_path, summary_path)
        result.append(
            {
                "paper_id": int(paper_id),
                "retrieval_order": int(order),
                "title": str(title or "Untitled paper"),
                "abstract": str(abstract or ""),
                "problem": string_or_none(summary.get("research_problem")),
                "why_it_matters": string_or_none(summary.get("why_it_matters")),
                "approach": string_or_none(summary.get("approach")),
            }
        )
    return result


def load_summary_snapshot(db_path: Path, stored_path: str | None) -> dict[str, Any]:
    if not stored_path:
        return {}
    path = Path(stored_path)
    if not path.is_absolute():
        path = db_path.resolve().parent.parent / path
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return {}
    merged = payload.get("merged") if isinstance(payload, dict) else None
    return merged if isinstance(merged, dict) else {}


def string_or_none(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    return value or None


def save_eval_decision(db_path: Path, *, queue_item_id: int, decision: str) -> dict[str, Any]:
    if decision not in DECISIONS:
        raise ValueError("Invalid MiniLM Eval decision")
    init_db(db_path)
    with connect_db(db_path) as connection:
        item = connection.execute(
            "SELECT eval_run_id, paper_id FROM minilm_eval_queue WHERE id = ?",
            (queue_item_id,),
        ).fetchone()
        if item is None:
            raise ValueError("MiniLM Eval queue item not found")
        connection.execute(
            """
            INSERT INTO minilm_eval_decisions (
                eval_run_id, paper_id, representative_queue_item_id, decision
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(eval_run_id, paper_id) DO UPDATE SET
                representative_queue_item_id = excluded.representative_queue_item_id,
                decision = excluded.decision,
                decided_at = datetime('now'),
                updated_at = datetime('now')
            """,
            (int(item[0]), int(item[1]), queue_item_id, decision),
        )
    return {"queue_item_id": queue_item_id, "paper_id": int(item[1]), "decision": decision}


def latest_unprocessed_source_run(db_path: Path, source: str) -> int | None:
    init_db(db_path)
    with connect_db(db_path) as connection:
        row = connection.execute(
            """
            SELECT sr.id FROM scout_runs sr
            LEFT JOIN minilm_eval_runs er ON er.source_run_id = sr.id
            WHERE sr.source = ? AND sr.completed_at IS NOT NULL AND er.id IS NULL
            ORDER BY sr.id DESC LIMIT 1
            """,
            (source,),
        ).fetchone()
    return int(row[0]) if row else None


def main() -> None:
    parser = argparse.ArgumentParser(description="Create an isolated MiniLM Eval queue")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--source",
        required=True,
        choices=("arxiv", "openalex", "semantic_scholar", "core"),
    )
    parser.add_argument("--candidate-limit", type=int, default=DEFAULT_CANDIDATE_LIMIT)
    args = parser.parse_args()
    source_run_id = latest_unprocessed_source_run(args.db, args.source)
    if source_run_id is None:
        print(json.dumps({"status": "no_unprocessed_run", "source": args.source}, sort_keys=True))
        return
    print(json.dumps(create_minilm_eval(args.db, source_run_id=source_run_id, candidate_limit=args.candidate_limit), sort_keys=True))


if __name__ == "__main__":
    main()
