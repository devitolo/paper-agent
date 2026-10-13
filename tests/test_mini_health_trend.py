import unittest

from scripts.mini_health_trend_summary import build_records, render_source_trends, render_table


class MiniHealthTrendTests(unittest.TestCase):
    def test_normalizes_source_warning_with_run_metrics(self):
        payload = {
            "latest_scout_runs_by_source": [
                {"id": 42, "source": "arxiv", "started_at": "2026-10-13 12:00:00", "candidate_count": 6, "eligible_count": 6, "recommendation_count": 0}
            ],
            "warnings": [{"level": "warning", "scout_run_id": 42, "message": "arxiv Scout run had only 6 eligible candidates and produced 0 recommendations."}],
        }
        records = build_records(payload, captured_at="2026-10-13T12:00:00Z")
        self.assertEqual(records[0]["category"], "underfilled_eligible_pool")
        self.assertEqual(records[0]["source"], "arxiv")
        self.assertEqual(records[0]["eligible"], 6)
        self.assertIn("underfilled_eligible_pool", render_table(records))

    def test_collection_failure_is_plumbing_record(self):
        records = build_records({"collection_error": "Missing production env file."}, captured_at="2026-10-13T12:00:00Z")
        self.assertEqual(records[0]["category"], "workflow_plumbing")
        self.assertEqual(records[0]["origin"], "workflow_plumbing")

    def test_source_trend_is_visible_in_text_output(self):
        runs = [
            {"source": "arxiv", "eligible_count": 1, "recommendation_count": 0},
            {"source": "arxiv", "eligible_count": 6, "recommendation_count": 0},
            {"source": "arxiv", "eligible_count": 12, "recommendation_count": 2},
        ]
        text = render_source_trends({"recent_scout_runs": runs})
        self.assertIn("arxiv", text)
        self.assertIn("eligible_count < 10 on 2 of 3", text)
        self.assertIn("recommendations=0 with eligible papers on 2 of 3", text)


if __name__ == "__main__":
    unittest.main()
