import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paper_agents.db import init_db
from paper_agents.sync_exchange import build_input_snapshot, connect_db, encoded, receive_result_batch


class NormalizedReceiveTests(unittest.TestCase):
    def test_mini_normalized_payload_creates_ui_review_rows(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            db = root / "data/paper_agent.db"
            db.parent.mkdir()
            init_db(db)
            (root / "config").mkdir()
            (root / "config/topics.yaml").write_text("topics: []")
            (root / "config/industry.json").write_text("{}")
            (root / "data/profile.json").write_text(
                '{"interests":[],"positive_signals":[],"negative_signals":[],"feedback_history":[]}'
            )
            snapshot = build_input_snapshot(db, root)
            canonical = "zenml:https://example.test/new-paper"
            summary = {"merged": {"research_problem": "new paper"}}
            normalized = {
                "papers": [{
                    "canonical_key": canonical,
                    "title": "A New ZenML Paper",
                    "abstract": "abstract",
                    "published": "2026-10-23",
                    "updated": None,
                    "doi": None,
                    "arxiv_id": None,
                    "authors_json": "[]",
                    "categories_json": "[]",
                    "first_discovered_at": "2026-10-23 22:00:00",
                    "last_discovered_at": "2026-10-23 22:00:00",
                }],
                "paper_sources": [{
                    "canonical_key": canonical,
                    "source": "zenml",
                    "source_id": "https://example.test/new-paper",
                    "url": "https://example.test/new-paper",
                    "pdf_url": None,
                    "discovered_at": "2026-10-23 22:00:00",
                    "metadata_json": "{}",
                }],
                "artifacts": [{
                    "canonical_key": canonical,
                    "artifact_type": "triage_summary",
                    "content": summary,
                    "model": "test-model",
                    "metadata_json": "{}",
                    "summary_hash": __import__("hashlib").sha256(encoded(summary)).hexdigest(),
                }],
                "evaluations": [{
                    "canonical_key": canonical,
                    "cycle_key": "batch:cycle:1",
                    "run_key": "batch:curator:1",
                    "score": 80.0,
                    "rationale": "matches",
                    "matched_signals_json": "[\"agent\"]",
                    "quality_threshold_met": 1,
                }],
                "recommendations": [{
                    "canonical_key": canonical,
                    "cycle_key": "batch:cycle:1",
                    "run_key": "batch:curator:1",
                    "recommendation_order": 1,
                    "rationale": "matches",
                }],
            }
            payload = {
                "schema": 1,
                "input_revision": snapshot["revision"],
                "producer_id": "mini-production",
                "environment": "production",
                "run_sequence": 1,
                "batch_id": "normalized-regression",
                "normalized": normalized,
                "results": [],
            }
            with patch.dict(os.environ, {
                "PAPER_AGENT_SYNC_PRODUCER": "mini-production",
                "PAPER_AGENT_SYNC_ENVIRONMENT": "production",
            }, clear=False):
                receipt = receive_result_batch(
                    db, encoded(payload), {"X-Paper-Sync-Idempotency": "normalized-regression"}, root
                )
            self.assertEqual(receipt["status"], "applied")
            with connect_db(db) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM papers WHERE canonical_key=?", (canonical,)).fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM paper_sources WHERE source_id=?", (canonical.split(":", 1)[1],)).fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM artifacts WHERE artifact_type='triage_summary'").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
