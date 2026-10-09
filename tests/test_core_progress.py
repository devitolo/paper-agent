from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import os
import ssl
import urllib.error
import urllib.parse

from paper_agents import db
from paper_agents.core_progress import CoreProgress
from paper_agents.scout import CoreSource, core_work_to_candidate, create_scout_source
from paper_agents.scout_agent import ScoutAgent, ScoutConfig


def work(number, **changes):
    return {
        "id": number,
        "title": f"Cloud incident diagnosis {number}",
        "abstract": "An incident diagnosis method.",
        "authors": [{"name": "Researcher"}],
        "publishedDate": "2026-09-01",
        **changes,
    }


def page(items, offset=0, next_offset=10, total_hits=100):
    return {
        "offset": offset,
        "next": next_offset,
        "results": items,
        "total_hits": total_hits,
        "search_id": "search-1",
    }


class CoreProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "db.sqlite"
        db.init_db(self.path)
        self.connection = db.connect_db(self.path)
        self.source = CoreSource(request_delay=3, verbose=False, api_key="test-secret")
        self.source.request_delay = 0
        self.now = datetime(2026, 9, 24, tzinfo=timezone.utc)

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def run_scout(self, responses, **kwargs):
        cycle = db.create_workflow_cycle(self.connection, mode="test", max_scout_attempts=1)
        with patch.object(self.source, "fetch_page", side_effect=responses) as fetch:
            result = ScoutAgent(self.source).run(
                self.connection,
                workflow_cycle_id=cycle,
                attempt_number=1,
                config=ScoutConfig(
                    topics=kwargs.pop("topics", ["incident"]),
                    core_progress_enabled=True,
                    **kwargs,
                ),
            )
        return result, fetch

    def progress(self, now=None):
        self.connection.commit()
        return CoreProgress(self.connection, self.source, now=now or self.now)

    def fetch(self, progress, responses, topics=None, maximum=30):
        with patch.object(self.source, "fetch_page", side_effect=responses) as fetch:
            result = progress.fetch(
                topics or ["incident"], freshness_months=24,
                max_candidates=maximum, errors=[],
            )
        return result, fetch

    def save(self, progress):
        cycle = db.create_workflow_cycle(self.connection, mode="test", max_scout_attempts=1)
        run = db.insert_scout_run(
            self.connection, workflow_cycle_id=cycle, attempt_number=1,
            source="core", target_candidates=20, max_candidates=30,
            freshness_months=24, topics=["incident"], guidance_id=None, diagnostics={},
        )
        with self.connection:
            progress.checkpoint(run)

    def test_source_is_explicit_and_progressive_only(self):
        with patch.dict(os.environ, {"PAPER_CORE_REQUEST_DELAY": "4.5"}):
            source = create_scout_source("core", request_delay=0, verbose=False)
        self.assertIsInstance(source, CoreSource)
        self.assertEqual(source.request_delay, 4.5)
        with self.assertRaisesRegex(RuntimeError, "progressive retrieval"):
            source.fetch(["incident"], 10, 24)

    def test_transport_requires_key_before_network(self):
        source = CoreSource(verbose=False, api_key="")
        with patch("urllib.request.urlopen") as call, self.assertRaisesRegex(RuntimeError, "CORE_API_KEY"):
            source.fetch_page("llm", offset=0, cutoff="2024-01-01", page_size=10)
        call.assert_not_called()

    def test_transport_uses_works_search_bearer_and_no_key_in_url(self):
        payload = {"totalHits": 1, "limit": 10, "offset": 20, "results": [],
                   "searchId": "s"}
        with patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(payload).encode())) as call:
            result = self.source.fetch_page("llm operations", offset=20,
                                            cutoff="2024-09-01", page_size=10)
        request = call.call_args.args[0]
        parsed = urllib.parse.urlparse(request.full_url)
        query = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(parsed.path, "/v3/search/works")
        self.assertEqual(query["offset"], ["20"])
        self.assertEqual(query["limit"], ["10"])
        self.assertEqual(query["stats"], ["false"])
        self.assertIn("yearPublished>=2024", query["q"][0])
        self.assertEqual(request.get_header("Authorization"), "Bearer test-secret")
        self.assertNotIn("test-secret", request.full_url)
        self.assertIsNone(result["next"])

    def test_candidate_normalizes_aliases_and_never_enables_pdf_download(self):
        candidate = core_work_to_candidate({
            "id": "core-1", "title": "  Useful   Paper ", "abstract": None,
            "authors": ["One", {"name": "Two"}], "year_published": 2026,
            "doi": "10.1/example", "arxiv_id": "2601.00001",
            "download_url": "https://files.example/paper.pdf",
            "source_fulltext_urls": ["https://repo.example/paper.pdf"],
            "links": [{"url": "https://landing.example/paper"}],
            "data_providers": [{"name": "Repository"}], "document_type": "article",
            "identifiers": {"local": "abc"},
        })
        self.assertEqual(candidate.source, "core")
        self.assertEqual(candidate.title, "Useful Paper")
        self.assertEqual(candidate.abstract, "")
        self.assertEqual(candidate.authors, ["One", "Two"])
        self.assertEqual(candidate.published, "2026")
        self.assertEqual(candidate.doi, "10.1/example")
        self.assertEqual(candidate.arxiv_id, "2601.00001")
        self.assertEqual(candidate.url, "https://landing.example/paper")
        self.assertIsNone(candidate.pdf_url)
        self.assertEqual(candidate.metadata["download_url"], "https://files.example/paper.pdf")

    def test_candidate_strips_html_and_jats_markup_from_text(self):
        candidate = core_work_to_candidate({
            "id": "core-html",
            "title": "<p>Agentic &amp; Multi-Agent Systems</p>",
            "abstract": "<jats:p>Systems use tools.</jats:p><p>They need governance.</p>",
        })
        self.assertEqual(candidate.title, "Agentic & Multi-Agent Systems")
        self.assertEqual(candidate.abstract, "Systems use tools. They need governance.")

    def test_identifier_list_supports_doi_and_arxiv_dedupe(self):
        candidate = core_work_to_candidate({
            "id": "core-2", "title": "Paper", "identifiers": [
                {"type": "DOI", "identifier": "10.2/test"},
                {"type": "arxiv", "value": "2602.00002"},
            ],
        })
        self.assertEqual(candidate.doi, "10.2/test")
        self.assertEqual(candidate.arxiv_id, "2602.00002")
        self.assertEqual(db.canonical_key_for_candidate(candidate.as_dict()), "arxiv:2602.00002")

    def test_next_run_uses_next_offset(self):
        first, _ = self.run_scout([page([work(1)], next_offset=1)])
        second, fetch = self.run_scout([page([work(2)], offset=1, next_offset=2)])
        self.assertEqual(fetch.call_args.kwargs["offset"], 1)
        self.assertEqual(first["eligible_count"], 1)
        self.assertEqual(second["eligible_count"], 1)

    def test_filtered_duplicate_and_missing_fields_page_still_checkpoints(self):
        db.upsert_paper(self.connection, core_work_to_candidate(work(1)).as_dict())
        self.connection.commit()
        result, _ = self.run_scout([page([
            work(1), work(2, publishedDate="2020-01-01"), work(3, title="")
        ], next_offset=3)])
        self.assertEqual(result["stored_count"], 0)
        ledger = json.loads(self.connection.execute(
            "SELECT page_json FROM core_page_dispositions"
        ).fetchone()[0])
        self.assertEqual([item["reason"] for item in ledger["dispositions"]],
                         ["duplicate", "too_old", "missing_title_or_source_id"])
        _, fetch = self.run_scout([page([], offset=3, next_offset=None, total_hits=3)])
        self.assertEqual(fetch.call_args.kwargs["offset"], 3)

    def test_request_budget_and_fair_rotation(self):
        progress = self.progress()
        self.fetch(progress, [page([])] * 3, topics=list("abcdef"))
        self.assertEqual(progress.diagnostics["attempts"], 3)
        self.assertEqual(progress.diagnostics["deferred_queries"], list("def"))
        self.save(progress)
        progress = self.progress()
        _, fetch = self.fetch(progress, [page([])] * 3, topics=list("abcdef"))
        self.assertEqual([call.args[0] for call in fetch.call_args_list], list("def"))

    def test_429_persists_core_cooldown_and_sanitizes_diagnostics(self):
        headers = {
            "X RateLimit Retry After": "120",
            "X-RateLimit-Remaining": "0",
            "Authorization": "Bearer must-not-leak",
        }
        error = urllib.error.HTTPError(
            "url", 429, "rate", headers,
            io.BytesIO(b'{"message":"Bearer test-secret limited","debug":"private"}'),
        )
        progress = self.progress()
        _, fetch = self.fetch(progress, [error], topics=list("abc"))
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(progress.diagnostics["stop_reason"], "source_cooldown")
        self.assertEqual(progress.diagnostics["not_before"], self.now.timestamp() + 120)
        diagnostic = progress.diagnostics["provider_error"]
        self.assertEqual(diagnostic["headers"], {
            "x-ratelimit-retry-after": "120", "x-ratelimit-remaining": "0",
        })
        self.assertNotIn("test-secret", json.dumps(diagnostic))
        self.assertNotIn("must-not-leak", json.dumps(diagnostic))
        later = self.progress(self.now + timedelta(seconds=60))
        self.fetch(later, [], topics=["incident"])
        self.assertEqual(later.diagnostics["stop_reason"], "source_cooldown")

    def test_transient_503_retries_once_without_advancing_offset(self):
        error = urllib.error.HTTPError("url", 503, "unavailable", {}, io.BytesIO())
        progress = self.progress()
        candidates, fetch = self.fetch(progress, [error, page([work(1)], next_offset=1)])
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(progress.diagnostics["attempts"], 2)
        self.assertEqual(progress.diagnostics["transient_retries"], [{
            "topic": "incident", "http_status": 503, "delay_seconds": 0,
        }])
        self.save(progress)
        progress = self.progress()
        _, fetch = self.fetch(progress, [page([], offset=1, next_offset=None, total_hits=1)])
        self.assertEqual(fetch.call_args.kwargs["offset"], 1)

    def test_transient_timeout_retries_once_and_repeated_failure_stops(self):
        timeout = urllib.error.URLError(TimeoutError("timed out"))
        progress = self.progress()
        candidates, fetch = self.fetch(progress, [timeout, page([work(1)])])
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(progress.diagnostics["transient_retries"], [{
            "topic": "incident", "transport_error": "TimeoutError", "delay_seconds": 0,
        }])

        progress = self.progress()
        _, fetch = self.fetch(progress, [
            urllib.error.URLError(TimeoutError("timed out")),
            urllib.error.URLError(TimeoutError("timed out")),
        ], topics=["a", "b"])
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(progress.diagnostics["stop_reason"], "source_error")
        self.assertEqual(progress.diagnostics["deferred_queries"], ["b"])

    def test_permanent_url_and_certificate_errors_are_not_retried(self):
        failures = [
            urllib.error.URLError("unknown url type"),
            urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate verify failed")),
        ]
        for error in failures:
            with self.subTest(reason=type(error.reason).__name__):
                progress = self.progress()
                _, fetch = self.fetch(progress, [error])
                self.assertEqual(fetch.call_count, 1)
                self.assertNotIn("transient_retries", progress.diagnostics)

    def test_write_failure_rolls_back_candidate_state_and_page(self):
        with patch("paper_agents.db.insert_scout_candidate", side_effect=RuntimeError("write failed")):
            with self.assertRaisesRegex(RuntimeError, "write failed"):
                self.run_scout([page([work(1)], next_offset=1)])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM core_search_state").fetchone()[0], 0)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM core_page_dispositions").fetchone()[0], 0)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM papers").fetchone()[0], 0)

    def test_malformed_payload_does_not_advance_and_http_is_outside_transaction(self):
        def malformed(*args, **kwargs):
            self.assertFalse(self.connection.in_transaction)
            raise ValueError("bad payload")
        result, _ = self.run_scout(malformed)
        self.assertTrue(result["errors"])
        state = json.loads(self.connection.execute(
            "SELECT value_json FROM core_search_state WHERE key != '@source'"
        ).fetchone()[0])
        self.assertEqual(state["deep"]["offset"], 0)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM core_page_dispositions").fetchone()[0], 0)

    def test_invalid_transport_payload_is_rejected(self):
        invalid = [
            {},
            {"total_hits": -1, "limit": 1, "offset": 0, "results": []},
            {"total_hits": 1, "limit": 1, "offset": 1, "results": []},
            {"total_hits": 2, "limit": 1, "offset": 0, "results": [work(1), work(2)]},
            {"total_hits": 1, "limit": 1, "offset": 0, "results": ["bad"]},
        ]
        for payload in invalid:
            with self.subTest(payload=payload), patch(
                "urllib.request.urlopen", return_value=io.BytesIO(json.dumps(payload).encode())
            ):
                with self.assertRaises(ValueError):
                    self.source.fetch_page("llm", offset=0, cutoff="2024-01-01", page_size=1)


if __name__ == "__main__":
    unittest.main()
