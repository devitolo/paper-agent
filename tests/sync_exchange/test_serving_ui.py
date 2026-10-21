import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paper_agents import web


class ServingUITests(unittest.TestCase):
    def test_nonprod_header_marks_every_shared_header_and_escapes_label(self):
        with patch.dict(os.environ, {"PAPER_NON_PRODUCTION": "1", "PAPER_UI_ENV_LABEL": "<candidate>"}):
            for page in ("review", "topics", "health"):
                header = web.render_app_header(page, "", "", page)
                self.assertIn("NON-PRODUCTION", header)
                self.assertIn("NON-PROD", header)
                self.assertIn("&lt;candidate&gt;", header)
                self.assertNotIn("<candidate>", header)

    def test_production_header_has_no_watermark(self):
        with patch.dict(os.environ, {"PAPER_NON_PRODUCTION": "0"}):
            self.assertNotIn("nonprod-watermark", web.render_app_header("Review", "", "", "review"))

    def test_serving_does_not_inspect_manual_model_readiness(self):
        with patch.dict(os.environ, {"PAPER_AGENT_PACKAGED": "1", "PAPER_AGENT_SERVING_ONLY": "1"}):
            with patch.object(web, "manual_scout_snapshot", side_effect=AssertionError("model call")):
                self.assertEqual(web.render_manual_scout_panel(Path("absent.db")), "")

    def test_serving_topic_agent_hidden_but_topics_still_editable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {"PAPER_AGENT_SERVING_ONLY": "1"}):
                markup = web.render_topics_page(config_path=root / "topics.yaml", industry_config_path=root / "industry.json", topic_mode="agent")
            self.assertNotIn("Ask Topic Agent", markup)
            self.assertNotIn("Propose topic changes", markup)
            self.assertIn("Topics", markup)

    def test_serving_background_profile_worker_never_calls_provider(self):
        with patch.dict(os.environ, {"PAPER_AGENT_SERVING_ONLY": "1"}):
            with patch.object(web, "apply_feedback_to_profile", side_effect=AssertionError("provider call")):
                web.run_profile_apply_worker(db_path=Path("absent.db"), structured_feedback_ids=[1])


if __name__ == "__main__":
    unittest.main()
