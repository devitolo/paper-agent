from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from paper_agents import db, web
from paper_agents.pipeline import run_daily_pipeline
from paper_agents.scout_agent import ScoutAgent, ScoutConfig
from paper_agents.scout_diagnostics import build_report, capture_report, load_report
from test_backend_v2 import RecordingSource, candidate


class ScoutDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "paper.db"
        db.init_db(self.path)
        self.connection = db.connect_db(self.path)
        self.cycle = db.create_workflow_cycle(self.connection, mode="test", max_scout_attempts=1)

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def scout(self, source="openalex", count=1):
        fake = RecordingSource([candidate(str(i), "AIOps diagnostics", source=source) for i in range(count)])
        fake.name = source
        result = ScoutAgent(fake).run(self.connection, workflow_cycle_id=self.cycle, attempt_number=1,
                                     config=ScoutConfig(topics=["AIOps"], max_refill_fetch_rounds=1))
        self.connection.commit()
        return result["scout_run_id"]

    def test_automatic_capture_for_all_scheduled_sources_and_zero_candidates(self):
        for source in ("arxiv", "openalex", "semantic_scholar"):
            with self.subTest(source=source):
                run_id = self.scout(source, count=0)
                report = load_report(self.path, run_id)
                self.assertEqual(report["capture_phase"], "scout_complete")
                self.assertEqual(report["counts"]["eligible"], 0)
                self.assertEqual(report["run"]["source"], source)
                self.assertIn("source_diagnostics", json.loads(report["run"]["diagnostics_json"]))
                self.assertIn(f'/health/scout-diagnostics/{run_id}', web.render_health_page(self.path))

    def test_snapshot_stays_bound_to_original_run_and_contains_exclusion_evidence(self):
        self.scout()
        self.cycle = db.create_workflow_cycle(self.connection, mode="test", max_scout_attempts=1)
        run_id = self.scout()
        report = load_report(self.path, run_id)
        self.assertEqual(report["exclusion_reasons"], {"previously_discovered": 1})
        self.assertEqual(report["candidates"][0]["source_query"], "AIOps")
        self.connection.execute("UPDATE papers SET title='changed later'")
        self.connection.commit()
        self.scout()
        self.assertEqual(load_report(self.path, run_id), report)

    def test_legacy_fallback_is_read_only_and_missing_run_is_none(self):
        run_id = self.scout()
        self.connection.execute("DROP TABLE scout_diagnostic_reports")
        self.connection.commit()
        before = self.path.read_bytes()
        report = load_report(self.path, run_id)
        self.assertEqual(report["capture_phase"], "requested")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIsNone(load_report(self.path, 99999))

    def test_capture_failure_does_not_rollback_pipeline_data(self):
        self.connection.execute("DROP TABLE scout_diagnostic_reports")
        self.connection.commit()
        with self.assertLogs("paper_agents.scout_diagnostics", level="ERROR"):
            run_id = self.scout()
        self.assertEqual(build_report(self.connection, run_id)["counts"]["candidates"], 1)
        self.assertFalse(self.connection.in_transaction)

    def test_sufficient_pool_only_captures_after_curator_if_no_recommendations(self):
        run_id = self.scout(count=20)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM scout_diagnostic_reports").fetchone()[0], 0)
        capture_report(self.connection, run_id, phase="curator_complete")
        self.connection.commit()
        self.assertEqual(load_report(self.path, run_id)["capture_phase"], "curator_complete")

    def test_saved_report_refreshes_when_later_curator_recovers(self):
        run_id = self.scout(count=20)
        curator_ids = []
        for attempt in (1, 2):
            curator_id = db.create_curator_run(
                self.connection, workflow_cycle_id=self.cycle, profile_version_id=None,
                scout_attempt_count=attempt, max_scout_attempts=2, min_quality_score=25,
                max_recommendations=3, model=None,
            )
            curator_ids.append(curator_id)
            if attempt == 1:
                self.connection.execute("UPDATE curator_runs SET requested_rescout=1 WHERE id=?", (curator_id,))
            else:
                candidate_id, paper_id = self.connection.execute(
                    "SELECT id, paper_id FROM scout_candidates WHERE scout_run_id=? ORDER BY id LIMIT 1", (run_id,)
                ).fetchone()
                db.insert_curator_evaluation(
                    self.connection, curator_run_id=curator_id, paper_id=paper_id,
                    scout_candidate_id=candidate_id, score=50, rationale="Recovered",
                    matched_signals=[], quality_threshold_met=True,
                )
                db.insert_recommendation(self.connection, curator_run_id=curator_id,
                                         paper_id=paper_id, recommendation_order=1, rationale="Recovered")
            capture_report(self.connection, run_id, phase="curator_complete")
            self.connection.commit()
            saved = load_report(self.path, run_id)
            self.assertEqual(saved["counts"]["recommendations"], attempt - 1)
            self.assertEqual([row["id"] for row in saved["curator_runs"]], curator_ids)
        current = build_report(self.connection, run_id, phase="curator_complete")
        self.assertEqual(saved["counts"], current["counts"])
        self.assertEqual(saved["curator_runs"], current["curator_runs"])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM scout_diagnostic_reports").fetchone()[0], 1)

    def test_arxiv_rate_limit_diagnostics_survive_source_failure(self):
        import urllib.error
        from paper_agents.scout import ArxivSource
        source = ArxivSource(retries=0, verbose=False)
        source.curl_path = None
        error = urllib.error.HTTPError("https://example.test", 429, "limited", {}, None)
        with patch("paper_agents.scout.urllib.request.urlopen", side_effect=error) as fetch:
            result = ScoutAgent(source).run(self.connection, workflow_cycle_id=self.cycle, attempt_number=1,
                                           config=ScoutConfig(topics=["one", "two"]))
        self.connection.commit()
        report = load_report(self.path, result["scout_run_id"])
        diagnostics = json.loads(report["run"]["diagnostics_json"])
        self.assertTrue(diagnostics["source_diagnostics"]["cooldown_active"])
        self.assertTrue(diagnostics["source_diagnostics"]["requests"])
        self.assertTrue(result["errors"])
        self.assertEqual(fetch.call_count, 1)

    def test_arxiv_partial_results_survive_cooldown_without_refill(self):
        import urllib.error
        from paper_agents.scout import ArxivSource
        source = ArxivSource(retries=0, request_delay=0, verbose=False)
        error = urllib.error.HTTPError("https://example.test", 429, "limited", {}, None)
        with patch.object(source, "_fetch_topic", side_effect=[[object()], error]) as fetch, patch(
            "paper_agents.scout.arxiv_entry_to_candidate", return_value=candidate("partial", "AIOps")
        ):
            result = ScoutAgent(source).run(self.connection, workflow_cycle_id=self.cycle, attempt_number=1,
                                           config=ScoutConfig(topics=["one", "two", "three"]))
        self.connection.commit()
        report = load_report(self.path, result["scout_run_id"])
        diagnostics = json.loads(report["run"]["diagnostics_json"])
        self.assertEqual(result["stored_count"], 1)
        self.assertEqual(diagnostics["refill"]["stop_reason"], "source_cooldown")
        self.assertEqual(diagnostics["source_diagnostics"]["skipped_topics"], 1)
        self.assertEqual(fetch.call_count, 2)

    def test_semantic_scholar_rate_limit_diagnostics_survive_source_failure(self):
        import urllib.error
        from paper_agents.scout import SemanticScholarSource
        source = SemanticScholarSource(retries=0, verbose=False)
        error = urllib.error.HTTPError("https://example.test", 429, "limited", {}, None)
        with patch("paper_agents.scout.urllib.request.urlopen", side_effect=error) as fetch:
            result = ScoutAgent(source).run(self.connection, workflow_cycle_id=self.cycle, attempt_number=1,
                                           config=ScoutConfig(topics=["one", "two"]))
        self.connection.commit()
        report = load_report(self.path, result["scout_run_id"])
        diagnostics = json.loads(report["run"]["diagnostics_json"])
        self.assertTrue(diagnostics["source_diagnostics"]["cooldown_active"])
        self.assertTrue(diagnostics["source_diagnostics"]["requests"])
        self.assertTrue(result["errors"])
        self.assertEqual(fetch.call_count, 1)

    def test_semantic_scholar_partial_results_survive_cooldown_without_refill(self):
        import urllib.error
        from paper_agents.scout import SemanticScholarSource
        source = SemanticScholarSource(retries=0, request_delay=0, verbose=False)
        error = urllib.error.HTTPError("https://example.test", 429, "limited", {}, None)
        with patch.object(source, "_fetch_topic", side_effect=[[object()], error]) as fetch, patch(
            "paper_agents.scout.semantic_scholar_paper_to_candidate", return_value=candidate("partial", "AIOps")
        ):
            result = ScoutAgent(source).run(self.connection, workflow_cycle_id=self.cycle, attempt_number=1,
                                           config=ScoutConfig(topics=["one", "two", "three"]))
        self.connection.commit()
        report = load_report(self.path, result["scout_run_id"])
        diagnostics = json.loads(report["run"]["diagnostics_json"])
        self.assertEqual(result["stored_count"], 1)
        self.assertEqual(diagnostics["refill"]["stop_reason"], "source_cooldown")
        self.assertEqual(diagnostics["source_diagnostics"]["skipped_topics"], 1)
        self.assertEqual(fetch.call_count, 2)

    def test_manual_source_does_not_save_report(self):
        run_id = self.scout("manual_backfill")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM scout_diagnostic_reports").fetchone()[0], 0)
        self.assertEqual(load_report(self.path, run_id)["capture_phase"], "requested")

    def test_pipeline_captures_post_curator_evidence(self):
        self.connection.commit()
        source = RecordingSource([candidate("pipeline", "AIOps diagnostics", source="openalex")])
        source.name = "openalex"
        with patch("paper_agents.pipeline.create_scout_source", return_value=source), patch("paper_agents.pipeline.load_profile", return_value={}):
            result = run_daily_pipeline(topics=["AIOps"], source_name="openalex", db_path=self.path,
                                        max_scout_attempts=1, min_quality_score=100)
        report = load_report(self.path, result["scout_results"][0]["scout_run_id"])
        self.assertEqual(report["capture_phase"], "curator_complete")
        self.assertEqual(report["counts"]["recommendations"], 0)
        self.assertEqual(len(report["curator_runs"]), 1)

    def test_empty_pipeline_report_keeps_curator_rescout_context(self):
        self.connection.commit()
        source = RecordingSource([])
        source.name = "semantic_scholar"
        with patch("paper_agents.pipeline.create_scout_source", return_value=source), patch("paper_agents.pipeline.load_profile", return_value={}):
            result = run_daily_pipeline(topics=["AIOps"], source_name="semantic_scholar", db_path=self.path,
                                        max_scout_attempts=2, min_quality_score=100)
        report = load_report(self.path, result["scout_results"][0]["scout_run_id"])
        self.assertEqual(report["counts"]["eligible"], 0)
        self.assertEqual(report["capture_phase"], "curator_complete")
        self.assertTrue(report["curator_runs"][0]["requested_rescout"])
        self.assertTrue(report["curator_runs"][0]["rescout_reason"])
        final_report = load_report(self.path, result["scout_results"][-1]["scout_run_id"])
        self.assertEqual(len(final_report["curator_runs"]), 1)
        self.assertEqual(final_report["curator_runs"][0]["scout_attempt_count"], 2)

    def test_diagnostic_script_uses_same_saved_report(self):
        run_id = self.scout()
        repo = Path(__file__).resolve().parents[1]
        result = subprocess.run(["bash", "scripts/diagnose_scout_run.sh", "openalex"],
                                cwd=repo, env={**os.environ, "PAPER_AGENT_REPO": str(repo),
                                               "PAPER_AGENT_DB": str(self.path)},
                                text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout), load_report(self.path, run_id))

    def test_diagnostics_page_escapes_report_content(self):
        run_id = self.scout()
        report = load_report(self.path, run_id)
        report["candidates"][0]["title"] = '</textarea><script>alert("test")</script>'
        html = web.render_scout_diagnostics_page(report)
        self.assertNotIn('</textarea><script>alert', html)
        self.assertIn('&lt;/textarea&gt;&lt;script&gt;', html)
        self.assertEqual(html.count('</textarea>'), 1)

    def test_diagnostics_handler_returns_copyable_page_and_rejects_invalid_ids(self):
        run_id = self.scout()
        handler_class = web.make_handler(self.path)
        handler = object.__new__(handler_class)
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.send_error = Mock()
        handler.wfile = io.BytesIO()
        handler.path = f"/health/scout-diagnostics/{run_id}"
        handler.do_GET()
        handler.send_response.assert_called_once_with(200)
        handler.send_header.assert_any_call("Content-Type", "text/html; charset=utf-8")
        self.assertFalse(any(call.args[0] == "Content-Disposition" for call in handler.send_header.call_args_list))
        handler.send_header.assert_any_call("Cache-Control", "no-store")
        from html import unescape
        import re
        html = handler.wfile.getvalue().decode("utf-8")
        report_text = re.search(r'<textarea[^>]*>(.*?)</textarea>', html, re.S).group(1)
        self.assertEqual(json.loads(unescape(report_text))["run"]["id"], run_id)
        self.assertIn("Copy report", html)
        self.assertIn("Report selected. Press Ctrl+C or Command+C", html)
        self.assertIn('target="_blank"', web.render_diagnostic_link({"scout_run_id": run_id}))
        for value in ("99999", "../.env", "1%20OR%201=1", "-1", "9" * 30):
            handler.path = f"/health/scout-diagnostics/{value}"
            handler.do_GET()
            handler.send_error.assert_called_with(404)
        self.assertEqual(web.render_warnings([]), "")


if __name__ == "__main__":
    unittest.main()
