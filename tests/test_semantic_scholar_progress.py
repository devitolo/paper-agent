from datetime import datetime, timedelta, timezone
from email.message import Message
import io
import json
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse

from paper_agents import db
from paper_agents.scout import SemanticScholarSource
from paper_agents.scout_agent import ScoutAgent, ScoutConfig
from paper_agents.semantic_scholar_progress import (
    SemanticScholarProgress, cooldown_seconds, transient_url_error,
)


def paper(number, **changes):
    return {
        "paperId": f"S2-{number}",
        "title": f"Cloud incident diagnosis {number}",
        "abstract": "An incident diagnosis method.",
        "authors": [{"name": "Researcher"}],
        "publicationDate": "2026-09-01",
        "externalIds": {},
        **changes,
    }


def page(items, offset=0, next_offset=10):
    return {"offset": offset, "next": next_offset, "data": items}


class SemanticScholarProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "db.sqlite"
        db.init_db(self.path)
        self.connection = db.connect_db(self.path)
        self.source = SemanticScholarSource(request_delay=0, verbose=False)
        self.now = datetime(2026, 9, 24, tzinfo=timezone.utc)

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def run_scout(self, responses, **kwargs):
        cycle = db.create_workflow_cycle(self.connection, mode="test", max_scout_attempts=1)
        with patch.object(self.source, "fetch_page", side_effect=responses) as fetch, patch(
            "paper_agents.scout_agent.topics_with_guidance", side_effect=lambda topics, _: topics
        ):
            result = ScoutAgent(self.source).run(
                self.connection,
                workflow_cycle_id=cycle,
                attempt_number=1,
                config=ScoutConfig(
                    topics=kwargs.pop("topics", ["incident"]),
                    semantic_scholar_progress_enabled=True,
                    **kwargs,
                ),
            )
        return result, fetch

    def progress(self, now=None):
        self.connection.commit()
        return SemanticScholarProgress(self.connection, self.source, now=now or self.now)

    def fetch(self, progress, responses, topics=None, maximum=30, freshness=24):
        with patch.object(self.source, "fetch_page", side_effect=responses) as fetch:
            result = progress.fetch(
                topics or ["incident"],
                freshness_months=freshness,
                max_candidates=maximum,
                errors=[],
            )
        return result, fetch

    def save(self, progress):
        cycle = db.create_workflow_cycle(self.connection, mode="test", max_scout_attempts=1)
        run = db.insert_scout_run(
            self.connection,
            workflow_cycle_id=cycle,
            attempt_number=1,
            source="semantic_scholar",
            target_candidates=20,
            max_candidates=30,
            freshness_months=24,
            topics=["incident"],
            guidance_id=None,
            diagnostics={},
        )
        with self.connection:
            progress.checkpoint(run)

    def test_next_run_uses_returned_next_offset(self):
        first, _ = self.run_scout([page([paper(1)], next_offset=10)])
        second, fetch = self.run_scout([page([paper(2)], offset=10, next_offset=20)])
        self.assertEqual(fetch.call_args.kwargs["offset"], 10)
        self.assertEqual(first["eligible_count"], 1)
        self.assertEqual(second["eligible_count"], 1)

    def test_filtered_and_duplicate_page_advances_and_records_every_result(self):
        db.upsert_paper(self.connection, {
            "source": "semantic_scholar", "source_id": "S2-1", "title": "Known",
            "abstract": "", "authors": [], "published": "2026-09-01", "updated": None,
            "url": "", "pdf_url": None, "doi": None, "arxiv_id": None,
            "categories": [], "primary_category": None, "metadata": {},
        })
        self.connection.commit()
        result, _ = self.run_scout([page([
            paper(1), paper(2, publicationDate="2020-01-01"), paper(3, title="")
        ], next_offset=10)])
        self.assertEqual(result["stored_count"], 0)
        ledger = json.loads(self.connection.execute(
            "SELECT page_json FROM semantic_scholar_page_dispositions"
        ).fetchone()[0])
        self.assertEqual(
            [item["reason"] for item in ledger["dispositions"]],
            ["duplicate", "too_old", "missing_title_or_source_id"],
        )
        _, fetch = self.run_scout([page([], offset=10, next_offset=None)])
        self.assertEqual(fetch.call_args.kwargs["offset"], 10)

    def test_candidate_write_failure_rolls_back_state_page_and_candidates(self):
        with patch("paper_agents.db.insert_scout_candidate", side_effect=RuntimeError("write failed")):
            with self.assertRaisesRegex(RuntimeError, "write failed"):
                self.run_scout([page([paper(1)], next_offset=10)])
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM semantic_scholar_search_state"
        ).fetchone()[0], 0)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM papers").fetchone()[0], 0)
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM semantic_scholar_page_dispositions"
        ).fetchone()[0], 0)

    def test_request_budget_and_fair_rotation(self):
        progress = self.progress()
        _, fetch = self.fetch(progress, [page([], next_offset=10)] * 3, topics=list("abcdef"))
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(progress.diagnostics["deferred_queries"], list("def"))
        self.save(progress)
        progress = self.progress()
        _, fetch = self.fetch(progress, [page([], next_offset=10)] * 3, topics=list("abcdef"))
        self.assertEqual([call.args[0] for call in fetch.call_args_list], list("def"))

    def test_429_cooldown_persists_and_honors_retry_after(self):
        headers = Message()
        headers["Retry-After"] = "120"
        headers["X-RateLimit-Remaining"] = "0"
        headers["X-Request-ID"] = "request-123"
        headers["X-Api-Key"] = "must-not-leak"
        headers["Set-Cookie"] = "also-must-not-leak"
        self.source.api_key = "secret-key"
        error = urllib.error.HTTPError(
            "url",
            429,
            "rate",
            headers,
            io.BytesIO(json.dumps({
                "message": "Rate limited; api_key=secret-key",
                "debug": "internal detail must not leak",
                "error": {
                    "code": "RATE_LIMITED",
                    "debug": "nested detail must not leak",
                },
            }).encode()),
        )
        progress = self.progress()
        _, fetch = self.fetch(progress, [error], topics=list("abc"))
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(progress.diagnostics["stop_reason"], "source_cooldown")
        provider_error = progress.diagnostics["provider_error"]
        self.assertEqual(provider_error["http_status"], 429)
        self.assertEqual(provider_error["headers"], {
            "retry-after": "120",
            "x-ratelimit-remaining": "0",
            "x-request-id": "request-123",
        })
        self.assertIn("[REDACTED]", provider_error["body"])
        self.assertNotIn("secret-key", provider_error["body"])
        self.assertNotIn("internal detail", provider_error["body"])
        self.assertNotIn("nested detail", provider_error["body"])
        self.assertIn("RATE_LIMITED", provider_error["body"])
        self.assertNotIn("must-not-leak", json.dumps(provider_error))
        progress = self.progress(self.now + timedelta(seconds=60))
        _, fetch = self.fetch(progress, [], topics=list("abc"))
        self.assertEqual(fetch.call_count, 0)
        self.assertEqual(progress.diagnostics["stop_reason"], "source_cooldown")

    def test_provider_error_body_is_bounded_and_handles_invalid_utf8(self):
        error = urllib.error.HTTPError(
            "url", 429, "rate", Message(), io.BytesIO(b"\xff" + b"x" * 5000)
        )
        progress = self.progress()
        self.fetch(progress, [error])
        provider_error = progress.diagnostics["provider_error"]
        self.assertTrue(provider_error["body_truncated"])
        self.assertLessEqual(len(provider_error["body"]), 1000)

    def test_transient_503_retries_once_without_advancing_offset(self):
        error = urllib.error.HTTPError("url", 503, "unavailable", Message(), io.BytesIO())
        progress = self.progress()
        with patch.object(
            self.source, "fetch_page", side_effect=[error, page([paper(1)], next_offset=10)]
        ) as fetch, patch("paper_agents.semantic_scholar_progress.time.sleep"):
            candidates = progress.fetch(
                ["incident"], freshness_months=24, max_candidates=10, errors=[]
            )
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual([call.kwargs["offset"] for call in fetch.call_args_list], [0, 0])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(progress.diagnostics["attempts"], 2)
        self.assertEqual(progress.diagnostics["transient_retries"], [{
            "topic": "incident", "http_status": 503, "delay_seconds": 0,
        }])

    def test_transient_timeout_retries_once(self):
        error = urllib.error.URLError(TimeoutError("timed out"))
        progress = self.progress()
        with patch.object(
            self.source, "fetch_page", side_effect=[error, page([paper(1)], next_offset=10)]
        ) as fetch, patch("paper_agents.semantic_scholar_progress.time.sleep"):
            candidates = progress.fetch(
                ["incident"], freshness_months=24, max_candidates=10, errors=[]
            )
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(progress.diagnostics["transient_retries"], [{
            "topic": "incident", "transport_error": "TimeoutError", "delay_seconds": 0,
        }])

    def test_repeated_timeout_stops_with_safe_transport_diagnostic(self):
        errors = []
        failures = [
            urllib.error.URLError(TimeoutError("first")),
            urllib.error.URLError(TimeoutError("second")),
        ]
        progress = self.progress()
        with patch.object(self.source, "fetch_page", side_effect=failures) as fetch, patch(
            "paper_agents.semantic_scholar_progress.time.sleep"
        ):
            candidates = progress.fetch(
                ["incident"], freshness_months=24, max_candidates=10, errors=errors
            )
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(candidates, [])
        self.assertEqual(progress.diagnostics["attempts"], 2)
        self.assertEqual(progress.diagnostics["transport_error"], {
            "error_type": "URLError", "reason_type": "TimeoutError",
        })
        self.assertEqual(errors, ["Semantic Scholar page failed (URLError): incident"])

    def test_certificate_error_is_not_retried(self):
        error = urllib.error.URLError(
            ssl.SSLCertVerificationError(1, "certificate verify failed")
        )
        self.assertFalse(transient_url_error(error))

    def test_changed_query_and_freshness_start_at_zero(self):
        progress = self.progress()
        self.fetch(progress, [page([], next_offset=10)])
        self.save(progress)
        progress = self.progress()
        _, fetch = self.fetch(progress, [page([], next_offset=None)], topics=["different"])
        self.assertEqual(fetch.call_args.kwargs["offset"], 0)
        progress = self.progress()
        _, fetch = self.fetch(progress, [page([], next_offset=None)], freshness=12)
        self.assertEqual(fetch.call_args.kwargs["offset"], 0)

    def test_end_of_results_waits_until_refresh(self):
        progress = self.progress()
        self.fetch(progress, [page([], next_offset=None)])
        self.save(progress)
        progress = self.progress(self.now + timedelta(days=1))
        _, fetch = self.fetch(progress, [])
        self.assertEqual(fetch.call_count, 0)
        self.assertEqual(progress.diagnostics["stop_reason"], "traversals_exhausted_until_refresh")

    def test_malformed_page_does_not_advance_offset_or_write_page(self):
        result, _ = self.run_scout([ValueError("bad next")])
        self.assertTrue(result["errors"])
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM semantic_scholar_search_state"
        ).fetchone()[0], 2)  # source control plus fair-rotation query state
        state = json.loads(self.connection.execute(
            "SELECT value_json FROM semantic_scholar_search_state WHERE key != '@source'"
        ).fetchone()[0])
        self.assertEqual(state["deep"]["offset"], 0)
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM semantic_scholar_page_dispositions"
        ).fetchone()[0], 0)

    def test_no_write_transaction_during_http(self):
        def response(*args, **kwargs):
            self.assertFalse(self.connection.in_transaction)
            return page([paper(1)], next_offset=None)
        self.run_scout(response)

    def test_weekly_freshness_sample_preserves_deep_offset(self):
        progress = self.progress()
        self.fetch(progress, [page([paper(1)], next_offset=10)])
        self.save(progress)

        progress = self.progress(self.now + timedelta(days=8))
        _, fetch = self.fetch(progress, [page([paper(2)], next_offset=10)])
        self.assertEqual(fetch.call_args.kwargs["offset"], 0)
        self.save(progress)

        progress = self.progress(self.now + timedelta(days=9))
        _, fetch = self.fetch(progress, [page([], offset=10, next_offset=20)])
        self.assertEqual(fetch.call_args.kwargs["offset"], 10)

    def test_normal_search_transport_sends_offset_and_never_bulk(self):
        response = io.BytesIO(json.dumps(page([paper(1)], offset=20, next_offset=30)).encode())
        with patch("urllib.request.urlopen", return_value=response) as request:
            self.source.fetch_page("llm", offset=20, cutoff="2024-09-01", page_size=10)
        url = request.call_args.args[0].full_url
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(urllib.parse.urlparse(url).path, "/graph/v1/paper/search")
        self.assertNotIn("/search/bulk", url)
        self.assertEqual(query["offset"], ["20"])
        self.assertEqual(query["limit"], ["10"])
        self.assertEqual(query["publicationDateOrYear"], ["2024-09-01:"])

    def test_invalid_payload_and_next_are_rejected(self):
        invalid = [
            {},
            {"offset": 0, "next": 0, "data": []},
            {"offset": 0, "next": 1001, "data": []},
            {"offset": 1, "next": None, "data": []},
            {"offset": 0, "next": None, "data": ["bad"]},
            {"offset": 0, "next": None, "data": [paper(1), paper(2)]},
        ]
        for payload in invalid:
            with self.subTest(payload=payload), patch(
                "urllib.request.urlopen", return_value=io.BytesIO(json.dumps(payload).encode())
            ):
                with self.assertRaises(ValueError):
                    self.source.fetch_page("llm", offset=0, cutoff="2024-01-01", page_size=1)

    def test_retry_after_date_and_invalid_fallback(self):
        self.assertEqual(cooldown_seconds("Thu, 24 Sep 2026 00:02:00 GMT", self.now), 120)
        for value in [None, "nonsense", "NaN", "-1"]:
            self.assertTrue(60 <= cooldown_seconds(value, self.now) <= 90)


if __name__ == "__main__":
    unittest.main()
