"""UI regression contracts using disposable databases and rendered HTML."""
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from paper_agents import db, web


class QueueControlsParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_controls = False
        self.values = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.in_controls = attrs.get("class") == "queue-controls"
        # form.submit() omits submit buttons: only persistent input controls
        # can retain view when changing a select.
        if self.in_controls and tag == "input" and "name" in attrs:
            self.values[attrs["name"]] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self.in_controls = False


class UIQueueRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "ui.db"
        db.init_db(self.path)
        self.connection = db.connect_db(self.path)
        self.addCleanup(self.connection.close)

    def seed(self, number):
        paper, _ = db.upsert_paper(self.connection, {
            "source": "arxiv", "source_id": f"ui-{number}",
            "title": f"Incident response {number}", "abstract": "Source orientation text.",
            "url": f"https://example.test/{number}",
        })
        cycle = db.create_workflow_cycle(self.connection, mode="test", max_scout_attempts=1)
        run = db.create_curator_run(
            self.connection, workflow_cycle_id=cycle, profile_version_id=None,
            scout_attempt_count=1, max_scout_attempts=1, min_quality_score=25,
            max_recommendations=3, model="qa")
        db.insert_curator_evaluation(
            self.connection, curator_run_id=run, paper_id=paper, scout_candidate_id=None,
            score=50, rationale="Relevant", matched_signals=[], quality_threshold_met=True)
        db.insert_recommendation(self.connection, curator_run_id=run, paper_id=paper,
                                 recommendation_order=1, rationale="Relevant")
        return paper

    def test_more_than_fifty_pending_papers_are_not_silently_inaccessible(self):
        for number in range(51):
            self.seed(number)
        self.connection.commit()
        cards = web.load_review_cards(self.path, filter_value="needs_review",
                                      source_value="all", sort_value="score")
        # No offset/cursor/pagination API exists: all pending cards must be
        # reachable. A pagination implementation can replace this assertion.
        self.assertEqual(len(cards), 51)

    def test_condensed_view_survives_select_filter_form_submission(self):
        self.connection.commit()
        parser = QueueControlsParser()
        parser.feed(web.render_review_queue(self.path, view_value="compact"))
        self.assertEqual(parser.values.get("view"), "compact",
                         "Select onchange calls form.submit(), omitting view submit buttons")

    def test_one_null_merged_summary_does_not_crash_entire_queue(self):
        paper = self.seed(1)
        artifact = Path(self.tmp.name) / "summary.json"
        artifact.write_text(json.dumps({"merged": None}))
        db.insert_artifact(self.connection, paper, artifact_type="triage_summary", path=artifact)
        self.connection.commit()
        html = web.render_review_queue(self.path)
        self.assertIn("Incident response 1", html)
        self.assertIn("Source orientation text.", html)

    def test_missing_abstract_only_summary_keeps_evidence_warning_on_fallback(self):
        paper = self.seed(1)
        db.insert_artifact(self.connection, paper, artifact_type="triage_summary",
                           path=Path(self.tmp.name) / "missing.json",
                           metadata={"abstract_only": True, "full_text_available": False})
        self.connection.commit()
        html = web.render_review_queue(self.path)
        self.assertIn("Source orientation text.", html)
        self.assertTrue("Abstract-only triage" in html,
                        "Missing summary file must not erase database evidence provenance")

    def test_http_feedback_save_round_trips_raw_text_and_moves_card_to_scored(self):
        paper = self.seed(1)
        self.connection.commit()
        content = "Decision: keep\nScore: 4.5\nReason: useful\n</textarea><script>alert(1)</script>"
        body = urlencode({"paper_id": paper, "action": "feedback", "notes": content,
                          "return_to": "/?filter=has_feedback&view=compact"}).encode()

        class RequestSocket:
            def __init__(self):
                self.response = bytearray()

            def makefile(self, *args, **kwargs):
                return io.BytesIO(b"POST /feedback HTTP/1.0\r\nContent-Type: application/x-www-form-urlencoded\r\n"
                                  + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)

            def sendall(self, data):
                self.response.extend(data)

        request = RequestSocket()
        with patch("paper_agents.web.start_profile_apply_worker") as worker:
            web.make_handler(self.path)(request, ("127.0.0.1", 1), object())
        self.assertIn(b"303 See Other", request.response)
        self.assertIn(b"view=compact", request.response)
        worker.assert_called_once()
        raw = self.connection.execute("SELECT content FROM raw_feedback WHERE paper_id=?", (paper,)).fetchone()[0]
        self.assertEqual(raw, content)
        scored = web.load_review_cards(self.path, filter_value="has_feedback", source_value="all", sort_value="score")
        pending = web.load_review_cards(self.path, filter_value="needs_review", source_value="all", sort_value="score")
        self.assertEqual([c["id"] for c in scored], [paper])
        self.assertEqual(pending, [])
        self.assertEqual(scored[0]["user_score"], 4.5)
        self.assertEqual(scored[0]["feedback_notes"], content)
        html = web.render_card(scored[0], view_value="full", return_to="/")
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertTrue("&lt;/textarea&gt;" in html)


class HealthTimezoneRegressionTests(unittest.TestCase):
    def test_current_utc_database_timestamp_is_not_seven_hours_in_future(self):
        class MiniClock(datetime):
            @classmethod
            def now(cls, tz=None):
                utc_now = datetime(2026, 9, 5, 14, 0, tzinfo=timezone.utc)
                if tz is not None:
                    return utc_now.astimezone(tz)
                return (utc_now - timedelta(hours=7)).replace(tzinfo=None)

        with patch("paper_agents.db.datetime", MiniClock):
            self.assertEqual(db._days_since("2026-09-05 14:00:00"), 0)
