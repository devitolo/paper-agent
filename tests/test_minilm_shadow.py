from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from paper_agents import db, web
from paper_agents.minilm_shadow import load_shadow_review, run_shadow_experiment, save_shadow_decision


class MiniLMShadowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / "paper_agent.db"
        db.init_db(self.db_path)
        with db.connect_db(self.db_path) as connection:
            cycle = connection.execute("INSERT INTO workflow_cycles(mode) VALUES ('test')").lastrowid
            self.source_run_id = connection.execute(
                """INSERT INTO scout_runs (
                       workflow_cycle_id, attempt_number, completed_at, source,
                       target_candidates, max_candidates, freshness_months
                   ) VALUES (?, 1, datetime('now'), 'openalex', 5, 5, 24)""",
                (cycle,),
            ).lastrowid
            for order in range(1, 6):
                paper_id = connection.execute(
                    "INSERT INTO papers(canonical_key,title,abstract,published) VALUES(?,?,?,?)",
                    (f"paper:{order}", f"Paper {order}", f"Abstract {order}", "2026-01-01"),
                ).lastrowid
                connection.execute(
                    "INSERT INTO scout_candidates(scout_run_id,paper_id,retrieval_order) VALUES(?,?,?)",
                    (self.source_run_id, paper_id, order),
                )

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def evaluator(candidate, profile):
        return {"score": float(candidate["paper_id"]), "rationale": f"score {candidate['paper_id']}"}

    def run_experiment(self):
        minilm_scores = {"Paper 3": 9.0, "Paper 2": 8.0, "Paper 4": 7.0, "Paper 5": 6.0, "Paper 1": 5.0}
        return run_shadow_experiment(
            self.db_path,
            source_run_id=self.source_run_id,
            input_limit=3,
            output_limit=2,
            min_quality_score=0,
            scorer=lambda title, abstract: minilm_scores[title],
            summarizer=lambda candidate: {
                "research_problem": f"Problem {candidate['paper_id']}",
                "why_it_matters": f"Why {candidate['paper_id']}",
                "approach": f"Approach {candidate['paper_id']}",
            },
            candidate_evaluator=self.evaluator,
        )

    def test_paired_paths_are_isolated_and_outputs_are_deduplicated(self):
        with db.connect_db(self.db_path) as connection:
            before = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                      for table in ("curator_runs", "curator_evaluations", "recommendations", "artifacts")}
        result = self.run_experiment()
        self.assertEqual(result["pool_count"], 5)
        self.assertEqual(result["baseline_input_count"], 3)
        self.assertEqual(result["minilm_input_count"], 3)
        self.assertEqual(result["output_count"], 3)
        with db.connect_db(self.db_path) as connection:
            paths = connection.execute(
                """SELECT path, paper_id, curator_input_position, final_output_position
                   FROM minilm_shadow_path_results ORDER BY path, curator_input_position"""
            ).fetchall()
            self.assertEqual([row[1] for row in paths if row[0] == "baseline"], [1, 2, 3])
            self.assertEqual([row[1] for row in paths if row[0] == "minilm"], [3, 2, 4])
            outputs = connection.execute(
                """SELECT paper_id, baseline_output_position, minilm_output_position
                   FROM minilm_shadow_outputs ORDER BY paper_id"""
            ).fetchall()
            self.assertEqual(len(outputs), 3)
            self.assertEqual(next(row for row in outputs if row[0] == 3)[1:], (1, 2))
            after = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                     for table in before}
        self.assertEqual(after, before)

    def test_blind_review_labels_shared_output_once_and_reports_both_paths(self):
        self.run_experiment()
        with db.connect_db(self.db_path) as connection:
            output_ids = dict(connection.execute("SELECT paper_id,id FROM minilm_shadow_outputs"))
        save_shadow_decision(self.db_path, output_id=output_ids[2], would_sample="no", usefulness=1)
        save_shadow_decision(self.db_path, output_id=output_ids[3], would_sample="yes", usefulness=5,
                             reason_tags=["strong_fit"])
        save_shadow_decision(self.db_path, output_id=output_ids[4], would_sample="maybe", usefulness=4)
        page = load_shadow_review(self.db_path)
        self.assertEqual(len(page["items"]), 3)
        metrics = {(row["source"], row["path"]): row for row in page["metrics"]}
        self.assertEqual(metrics[("openalex", "baseline")]["yes_count"], 1)
        self.assertEqual(metrics[("openalex", "minilm")]["yes_count"], 1)
        self.assertEqual(metrics[("openalex", "minilm")]["sample_count"], 2)
        self.assertEqual(page["comparisons"], [{"source": "openalex", "yes_gain": 0, "sample_gain": 1}])
        rendered = web.render_retrieval_experiment_page(self.db_path)
        self.assertIn("Would you read this?", rendered)
        self.assertIn(">Read</button>", rendered)
        self.assertIn(">Maybe</button>", rendered)
        self.assertIn(">Skip</button>", rendered)
        self.assertNotIn("Usefulness", rendered)
        self.assertNotIn("Optional reasons", rendered)
        self.assertNotIn("data-usefulness", rendered)
        self.assertNotIn("data-reason", rendered)
        self.assertIn("yes:'5', maybe:'3', no:'1'", rendered)
        self.assertNotIn("minilm_score", rendered)
        self.assertNotIn("curator_input_position", rendered)

    def test_invalid_label_rejected(self):
        self.run_experiment()
        with db.connect_db(self.db_path) as connection:
            output_id = connection.execute("SELECT id FROM minilm_shadow_outputs LIMIT 1").fetchone()[0]
        with self.assertRaisesRegex(ValueError, "between 1 and 5"):
            save_shadow_decision(self.db_path, output_id=output_id, would_sample="yes", usefulness=6)


if __name__ == "__main__":
    unittest.main()
