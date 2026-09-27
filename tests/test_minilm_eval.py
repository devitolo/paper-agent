from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from paper_agents import db, web
from paper_agents.minilm_eval import create_minilm_eval, minilm_bucket, save_eval_decision


class MiniLMEvalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / "paper_agent.db"
        db.init_db(self.db_path)
        with db.connect_db(self.db_path) as connection:
            cycle = connection.execute(
                "INSERT INTO workflow_cycles(mode) VALUES ('test')"
            ).lastrowid
            self.source_run_id = connection.execute(
                """
                INSERT INTO scout_runs (
                    workflow_cycle_id, completed_at, source, target_candidates,
                    max_candidates, freshness_months
                ) VALUES (?, datetime('now'), 'openalex', 3, 3, 12)
                """,
                (cycle,),
            ).lastrowid
            for order, (title, abstract) in enumerate(
                (("First", "alpha"), ("Second", "beta"), ("No abstract", None)), 1
            ):
                paper_id = connection.execute(
                    "INSERT INTO papers(canonical_key,title,abstract) VALUES(?,?,?)",
                    (f"paper:{order}", title, abstract),
                ).lastrowid
                connection.execute(
                    "INSERT INTO scout_candidates(scout_run_id,paper_id,retrieval_order) VALUES(?,?,?)",
                    (self.source_run_id, paper_id, order),
                )

    def tearDown(self):
        self.temporary.cleanup()

    def test_same_pool_has_two_rankings_and_does_not_touch_production_tables(self):
        scores = {"First": -5.0, "Second": 2.0}
        with db.connect_db(self.db_path) as connection:
            before = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("curator_runs", "curator_evaluations", "recommendations", "raw_feedback")
            }

        result = create_minilm_eval(
            self.db_path,
            source_run_id=self.source_run_id,
            scorer=lambda title, abstract: scores[title],
        )

        self.assertEqual(result["candidate_count"], 3)
        self.assertEqual(result["queue_item_count"], 6)
        with db.connect_db(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT recommendation_mode, rank_position, title_snapshot, minilm_bucket
                FROM minilm_eval_queue ORDER BY recommendation_mode, rank_position
                """
            ).fetchall()
            baseline = [(row[2], row[3]) for row in rows if row[0] == "baseline"]
            assisted = [(row[2], row[3]) for row in rows if row[0] == "minilm_assisted"]
            self.assertEqual([title for title, _ in baseline], ["First", "Second", "No abstract"])
            self.assertEqual([title for title, _ in assisted], ["Second", "First", "No abstract"])
            self.assertEqual({title for title, _ in baseline}, {title for title, _ in assisted})
            self.assertEqual(dict(baseline)["No abstract"], "Insufficient metadata")
            metadata = json.loads(connection.execute(
                "SELECT candidate_pool_snapshot_json FROM minilm_eval_runs"
            ).fetchone()[0])
            self.assertEqual(metadata["unreviewed_semantics"], "unknown")
            after = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in before
            }
        self.assertEqual(after, before)
        self.assertEqual(
            create_minilm_eval(self.db_path, source_run_id=self.source_run_id, scorer=lambda *_: 0)["status"],
            "already_exists",
        )

    def test_decision_is_one_per_run_and_paper_while_unreviewed_has_no_row(self):
        run = create_minilm_eval(
            self.db_path, source_run_id=self.source_run_id, scorer=lambda *_: 0.0
        )
        with db.connect_db(self.db_path) as connection:
            queue_ids = [row[0] for row in connection.execute(
                "SELECT id FROM minilm_eval_queue WHERE eval_run_id=? ORDER BY id", (run["eval_run_id"],)
            ).fetchall()]
        save_eval_decision(self.db_path, queue_item_id=queue_ids[0], decision="maybe")
        save_eval_decision(self.db_path, queue_item_id=queue_ids[0], decision="skip")
        with db.connect_db(self.db_path) as connection:
            decisions = connection.execute(
                "SELECT decision FROM minilm_eval_decisions"
            ).fetchall()
        self.assertEqual([row[0] for row in decisions], ["skip"])
        with self.assertRaisesRegex(ValueError, "Invalid"):
            save_eval_decision(self.db_path, queue_item_id=queue_ids[0], decision="negative")

    def test_blind_page_deduplicates_papers_and_hides_ranking_metadata(self):
        create_minilm_eval(self.db_path, source_run_id=self.source_run_id, scorer=lambda *_: 1.0)
        page = web.render_minilm_eval_page(self.db_path)
        self.assertEqual(page.count('<article class="minilm-eval-card">'), 3)
        self.assertIn("Send to Curator", page)
        self.assertNotIn("Unreviewed means unknown", page)
        self.assertIn("No abstract available.", page)
        self.assertNotIn("Prioritize", page)
        self.assertNotIn("minilm_assisted", page)
        self.assertNotIn("baseline_rank", page)

    def test_bucket_boundaries(self):
        self.assertEqual(minilm_bucket(None), "Insufficient metadata")
        self.assertEqual(minilm_bucket(-3), "Lower priority")
        self.assertEqual(minilm_bucket(-8), "Low topical match")
        self.assertEqual(minilm_bucket(-2.99), "Prioritize")


if __name__ == "__main__":
    unittest.main()
