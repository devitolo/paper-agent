import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paper_agents.web import classify_attention_message, render_health_page, render_needs_attention_items


class NeedsAttentionTests(unittest.TestCase):
    def test_processing_item_has_evidence_and_confidence(self):
        with patch.dict(os.environ, {"PAPER_AGENT_ORCHESTRATOR_STATUS_FILE": "/missing/status.json"}):
            items = render_needs_attention_items({"warnings": [{"level": "warning", "message": "arxiv returned 0 candidates"}]})
        processing = next(item for item in items if item["domain"] == "processing")
        self.assertEqual(processing["state"], "observed")
        self.assertEqual(processing["confidence"], "confirmed")
        self.assertIn("arxiv", processing["evidence"])

    def test_delivery_state_is_separate_and_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            status = Path(directory) / "status.json"
            status.write_text(json.dumps({"updated_at": "2020-01-01T00:00:00+00:00", "input_revision": "r1", "overall_result": "completed"}))
            with patch.dict(os.environ, {"PAPER_AGENT_ORCHESTRATOR_STATUS_FILE": str(status)}):
                items = render_needs_attention_items({"warnings": []})
        delivery = next(item for item in items if item["domain"] == "delivery")
        self.assertEqual(delivery["state"], "stale")
        self.assertEqual(delivery["affected_scope"], "Mini-to-AWS delivery")

    def test_health_page_contains_panel(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "paper.db"
            with patch.dict(os.environ, {"PAPER_AGENT_ORCHESTRATOR_STATUS_FILE": str(Path(directory) / "missing.json")}):
                page = render_health_page(db)
        self.assertIn("Needs Attention", page)
        self.assertIn("Mini-to-AWS delivery", page)

    def test_jobs_and_infrastructure_tabs_render(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "paper.db"
            with patch.dict(os.environ, {"PAPER_AGENT_ORCHESTRATOR_STATUS_FILE": str(Path(directory) / "missing.json")}):
                jobs = render_health_page(db, tab="jobs")
                infrastructure = render_health_page(db, tab="infrastructure")
        self.assertIn("Operational job history", jobs)
        self.assertIn("Aws Delivery State", infrastructure)
        self.assertIn("placeholder", infrastructure)
        analytics = render_health_page(db, tab="analytics")
        self.assertIn("Pipeline analytics", analytics)

    def test_rate_limit_and_unknown_rules_are_deterministic(self):
        self.assertEqual(classify_attention_message("provider returned HTTP 429" )[0], "rate_limit")
        self.assertEqual(classify_attention_message("unrecognized condition")[3], "unknown")


if __name__ == "__main__":
    unittest.main()
