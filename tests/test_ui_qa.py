"""UI regression contracts using disposable databases and rendered HTML."""
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlencode, parse_qs, urlparse

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

    def test_queue_defaults_include_reviewed_papers_from_all_sources_newest_first(self):
        older = self.seed(1)
        newer = self.seed(2)
        self.connection.execute("UPDATE papers SET first_discovered_at='2026-01-01 00:00:00' WHERE id=?", (older,))
        self.connection.execute("UPDATE papers SET first_discovered_at='2026-02-01 00:00:00' WHERE id=?", (newer,))
        self.connection.execute("UPDATE paper_sources SET source='openalex' WHERE paper_id=?", (older,))
        self.connection.execute("INSERT INTO feedback (paper_id, status, notes) VALUES (?, 'reviewed', 'Score: 5')", (older,))
        self.connection.commit()

        for choices in ({}, {"filter_value": "invalid", "sort_value": "invalid", "source_value": "invalid"}):
            with self.subTest(choices=choices):
                html = web.render_review_queue(self.path, **choices)
                self.assertIn('<option value="all" selected>All papers</option>', html)
                self.assertIn('<option value="all" selected>All sources</option>', html)
                self.assertIn('<option value="latest" selected>Newest</option>', html)
                self.assertLess(html.index('Incident response 2'), html.index('Incident response 1'))

    def test_get_queue_defaults_and_explicit_url_selections(self):
        self.seed(1)
        self.connection.commit()

        class RequestSocket:
            def __init__(self, query):
                self.query = query
                self.response = bytearray()

            def makefile(self, *args, **kwargs):
                return io.BytesIO(f"GET /{self.query} HTTP/1.0\r\n\r\n".encode())

            def sendall(self, data):
                self.response.extend(data)

        for query, selections in (
            ("", (("all", "All papers"), ("all", "All sources"), ("latest", "Newest"))),
            ("?filter=needs_review&source=arxiv&sort=score", (("needs_review", "Needs review"), ("arxiv", "arXiv"), ("score", "Highest score"))),
            ("?filter=has_feedback&source=all&sort=score", (("has_feedback", "Scored"), ("all", "All sources"), ("score", "Highest score"))),
            ("?filter=reviewed&sort=invalid", (("all", "All papers"), ("all", "All sources"), ("latest", "Newest"))),
        ):
            with self.subTest(query=query):
                request = RequestSocket(query)
                web.make_handler(self.path)(request, ("127.0.0.1", 1), object())
                self.assertIn(b"200 OK", request.response)
                html = request.response.decode()
                for value, label in selections:
                    self.assertIn(f'<option value="{value}" selected>{label}</option>', html)

    def test_more_than_fifty_pending_papers_are_not_silently_inaccessible(self):
        expected = {self.seed(number) for number in range(121)}
        other = self.seed(122)
        self.connection.execute("UPDATE paper_sources SET source='openalex' WHERE paper_id=?", (other,))
        self.connection.commit()
        for source in ("all", "arxiv"):
            for sort in ("score", "latest"):
                matching = expected | {other} if source == "all" else expected
                seen = []
                for page in (1, 2, 3):
                    result = web.load_review_page(self.path, filter_value="needs_review",
                                                  source_value=source, sort_value=sort, page=page)
                    self.assertEqual(result["total"], len(matching))
                    self.assertEqual(result["pages"], 3)
                    self.assertEqual(len(result["cards"]), 50 if page < 3 else len(matching) - 100)
                    seen.extend(card["id"] for card in result["cards"])
                    navigation = web.render_queue_pagination(result, "needs_review", source, sort, "compact")

                    class Links(HTMLParser):
                        def handle_starttag(parser, tag, attrs):
                            attrs = dict(attrs)
                            if tag == "a":
                                query = parse_qs(urlparse(attrs["href"]).query)
                                self.assertEqual(query["filter"], ["needs_review"])
                                self.assertEqual(query["source"], [source])
                                self.assertEqual(query["sort"], [sort])
                                self.assertEqual(query["view"], ["compact"])
                                self.assertIn(int(query["page"][0]), (page - 1, page + 1))

                    Links().feed(navigation)
                self.assertEqual(set(seen), matching)
                self.assertEqual(len(seen), len(set(seen)))
                self.assertEqual(seen, sorted(matching, reverse=True))
        self.assertIn("122 papers |", web.render_review_queue(self.path, page_value=2))

    def test_pagination_handles_invalid_pages_and_empty_filters(self):
        for number in range(51):
            self.seed(number)
        self.connection.commit()
        for value, expected_page in [("bad", 1), ("-1", 1), ("0", 1), ("999999999999999999999", 2)]:
            result = web.load_review_page(self.path, filter_value="needs_review", source_value="all",
                                          sort_value="score", page=value)
            self.assertEqual(result["page"], expected_page)
        empty = web.load_review_page(self.path, filter_value="has_feedback", source_value="all", sort_value="score", page=2)
        self.assertEqual((empty["total"], empty["page"], empty["cards"]), (0, 1, []))

    def test_scored_raw_feedback_pagination_counts_papers_not_feedback_edits(self):
        expected = {self.seed(number) for number in range(51)}
        pending = self.seed(100)
        for paper in expected:
            for edit in range(2):
                self.connection.execute(
                    "INSERT INTO raw_feedback (paper_id, content, content_hash) VALUES (?, ?, ?)",
                    (paper, f"Saved raw feedback edit {edit}", f"qa-{paper}-{edit}"))
        self.connection.commit()
        seen = []
        for page, count in [(1, 50), (2, 1)]:
            result = web.load_review_page(self.path, filter_value="has_feedback", source_value="arxiv",
                                          sort_value="latest", page=page)
            self.assertEqual(result["total"], 51)
            self.assertEqual(len(result["cards"]), count)
            self.assertTrue(all(card["feedback_notes"] == "Saved raw feedback edit 1" for card in result["cards"]))
            seen.extend(card["id"] for card in result["cards"])
        self.assertEqual(set(seen), expected)
        self.assertEqual(len(seen), len(expected))
        result = web.load_review_page(self.path, filter_value="needs_review", source_value="arxiv", sort_value="score")
        self.assertEqual(result["total"], 1)
        self.assertEqual([card["id"] for card in result["cards"]], [pending])

    def test_http_view_toggle_overrides_persistent_view_and_keeps_page(self):
        for number in range(51):
            self.seed(number)
        self.connection.commit()

        class RequestSocket:
            def __init__(self, query):
                self.query = query
                self.response = bytearray()

            def makefile(self, *args, **kwargs):
                return io.BytesIO(f"GET /?{self.query} HTTP/1.0\r\n\r\n".encode())

            def sendall(self, data):
                self.response.extend(data)

        for query, expected in [("view=compact&page=2&sort=latest&source=arxiv", "compact"),
                                ("view=compact&page=2&view=full", "full"),
                                ("view=full&page=2&view=compact", "compact")]:
            request = RequestSocket(query)
            web.make_handler(self.path)(request, ("127.0.0.1", 1), object())
            self.assertIn(b"200 OK", request.response)
            parser = QueueControlsParser()
            parser.feed(request.response.decode())
            self.assertEqual(parser.values["view"], expected)
            self.assertEqual(parser.values["page"], "2")
            self.assertIn(b"page=2", request.response)

    def test_feedback_on_last_page_returns_to_valid_page_with_view_preserved(self):
        for number in range(51):
            self.seed(number)
        self.connection.commit()
        page = web.load_review_page(self.path, filter_value="needs_review", source_value="arxiv",
                                    sort_value="score", page=2)
        card = page["cards"][0]
        body = urlencode({"paper_id": card["id"], "recommendation_id": card["recommendation_id"],
                          "action": "feedback", "notes": "Decision: keep\nScore: 4.5",
                          "return_to": "/?filter=needs_review&source=arxiv&sort=score&view=compact&page=2"}).encode()

        class RequestSocket:
            def __init__(self, request):
                self.request = request
                self.response = bytearray()

            def makefile(self, *args, **kwargs):
                return io.BytesIO(self.request)

            def sendall(self, data):
                self.response.extend(data)

        request = RequestSocket(b"POST /feedback HTTP/1.0\r\n" +
                                f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        with patch("paper_agents.web.start_profile_apply_worker"):
            web.make_handler(self.path)(request, ("127.0.0.1", 1), object())
        self.assertIn(b"303 See Other", request.response)
        location = next(line.split(": ", 1)[1] for line in request.response.decode().splitlines()
                        if line.startswith("Location:"))
        follow = RequestSocket(f"GET {location} HTTP/1.0\r\n\r\n".encode())
        web.make_handler(self.path)(follow, ("127.0.0.1", 1), object())
        self.assertIn(b"200 OK", follow.response)
        controls = QueueControlsParser()
        controls.feed(follow.response.decode())
        self.assertEqual(controls.values, {"view": "compact", "page": "1"})
        self.assertIn(b"50 papers |", follow.response)

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

    def test_malformed_summary_shapes_preserve_known_provenance(self):
        path = Path(self.tmp.name) / "malformed.json"
        for payload in ({"merged": None}, {"merged": []}, {"merged": 5}, {"merged": "text"}, [], None):
            path.write_text(json.dumps(payload))
            summary = web.load_summary({"path": path, "metadata": {"full_text_available": False}})
            self.assertTrue(summary["abstract_only"])
            self.assertEqual(summary["source_type"], "source_abstract")
        for content in (b"not json", b"\xff\xfe"):
            path.write_bytes(content)
            self.assertTrue(web.load_summary({"path": path, "metadata": {"abstract_only": True}})["abstract_only"])

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
            self.assertEqual(db._days_since("2026-09-05T07:00:00-07:00"), 0)
            self.assertEqual(db._days_since("2026-09-05T14:00:00Z"), 0)
            self.assertEqual(db._days_since("2026-09-04 14:00:00"), 1)
            self.assertEqual(db._days_since("2026-09-05"), round(14 / 24, 2))
            self.assertIsNone(db._days_since(None))
            self.assertIsNone(db._days_since("invalid"))
