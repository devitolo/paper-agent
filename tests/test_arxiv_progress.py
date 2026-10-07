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
import xml.etree.ElementTree as ET

from paper_agents import db
from paper_agents.arxiv_progress import ArxivProgress, transient_url_error
from paper_agents.scout import ArxivSource
from paper_agents.scout_agent import ScoutAgent, ScoutConfig


def entry(number, *, published="2026-09-01", title=None):
    value = title if title is not None else f"Cloud incident diagnosis {number}"
    return ET.fromstring(f"""
    <entry xmlns="http://www.w3.org/2005/Atom">
      <id>https://arxiv.org/abs/2609.{number:05d}v1</id>
      <title>{value}</title><summary>Incident diagnosis research.</summary>
      <published>{published}T00:00:00Z</published><updated>{published}T00:00:00Z</updated>
      <author><name>Researcher</name></author><category term="cs.SE"/>
      <link rel="alternate" href="https://arxiv.org/abs/2609.{number:05d}v1"/>
      <link title="pdf" href="https://arxiv.org/pdf/2609.{number:05d}v1"/>
    </entry>
    """)


def page(items, offset=0, next_offset=10):
    return {"entries": items, "offset": offset, "next": next_offset}


class ArxivProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "db.sqlite"
        db.init_db(self.path)
        self.connection = db.connect_db(self.path)
        self.source = ArxivSource(request_delay=0, retries=0, verbose=False)
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
                    arxiv_progress_enabled=True,
                    **kwargs,
                ),
            )
        return result, fetch

    def progress(self, now=None):
        self.connection.commit()
        return ArxivProgress(self.connection, self.source, now=now or self.now)

    def test_next_run_uses_next_offset_and_finds_new_candidate(self):
        first, _ = self.run_scout([page([entry(1)], next_offset=10)])
        second, fetch = self.run_scout([page([entry(2)], offset=10, next_offset=20)])
        self.assertEqual(fetch.call_args.kwargs["offset"], 10)
        self.assertEqual(first["eligible_count"], 1)
        self.assertEqual(second["eligible_count"], 1)

    def test_duplicate_and_old_page_advances_with_ledger(self):
        db.upsert_paper(self.connection, {
            "source": "arxiv", "source_id": "2609.00001v1", "title": "Known",
            "url": "https://arxiv.org/abs/2609.00001v1",
        })
        self.connection.commit()
        result, _ = self.run_scout([page([entry(1), entry(2, published="2020-01-01")])])
        self.assertEqual(result["stored_count"], 0)
        ledger = json.loads(self.connection.execute(
            "SELECT page_json FROM arxiv_page_dispositions"
        ).fetchone()[0])
        self.assertEqual(
            [item["reason"] for item in ledger["dispositions"]],
            ["duplicate", "too_old"],
        )
        _, fetch = self.run_scout([page([entry(3)], offset=10, next_offset=20)])
        self.assertEqual(fetch.call_args.kwargs["offset"], 10)

    def test_weekly_refresh_does_not_replace_deep_offset(self):
        self.run_scout([page([entry(1)], next_offset=10)])
        future = datetime.now(timezone.utc) + timedelta(days=8)
        with patch("paper_agents.arxiv_progress.datetime") as clock:
            clock.now.return_value = future
            clock.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
            _, fetch = self.run_scout([page([entry(2)], offset=0, next_offset=10)])
        self.assertEqual(fetch.call_args.kwargs["offset"], 0)
        _, fetch = self.run_scout([page([entry(3)], offset=10, next_offset=20)])
        self.assertEqual(fetch.call_args.kwargs["offset"], 10)

    def test_429_sets_source_cooldown_without_advancing(self):
        headers = Message(); headers["Retry-After"] = "120"
        error = urllib.error.HTTPError("url", 429, "rate", headers, io.BytesIO())
        result, fetch = self.run_scout([error], topics=["a", "b"])
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(result["errors"], [])
        state = json.loads(self.connection.execute(
            "SELECT value_json FROM arxiv_search_state WHERE key='@source'"
        ).fetchone()[0])
        self.assertGreater(state["not_before"], 0)

    def test_transient_503_retries_once_without_advancing_offset(self):
        error = urllib.error.HTTPError("url", 503, "unavailable", Message(), io.BytesIO())
        progress = self.progress()
        with patch.object(
            self.source, "fetch_page", side_effect=[error, page([entry(1)], next_offset=10)]
        ) as fetch, patch("paper_agents.arxiv_progress.time.sleep"):
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

    def test_repeated_transient_503_stops_after_one_retry(self):
        errors = []
        failures = [
            urllib.error.HTTPError("url", 503, "unavailable", Message(), io.BytesIO()),
            urllib.error.HTTPError("url", 503, "unavailable", Message(), io.BytesIO()),
        ]
        progress = self.progress()
        with patch.object(self.source, "fetch_page", side_effect=failures) as fetch, patch(
            "paper_agents.arxiv_progress.time.sleep"
        ):
            candidates = progress.fetch(
                ["incident"], freshness_months=24, max_candidates=10, errors=errors
            )
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(candidates, [])
        self.assertEqual(progress.diagnostics["attempts"], 2)
        self.assertEqual(progress.diagnostics["stop_reason"], "source_error")
        self.assertEqual(errors, ["arXiv HTTP 503: incident"])
        self.assertEqual(progress.states[next(k for k in progress.states if k != "@source")]["deep"]["offset"], 0)

    def test_transient_timeout_retries_once(self):
        error = urllib.error.URLError(TimeoutError("timed out"))
        progress = self.progress()
        with patch.object(
            self.source, "fetch_page", side_effect=[error, page([entry(1)], next_offset=10)]
        ) as fetch, patch("paper_agents.arxiv_progress.time.sleep"):
            candidates = progress.fetch(
                ["incident"], freshness_months=24, max_candidates=10, errors=[]
            )
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(progress.diagnostics["transient_retries"], [{
            "topic": "incident", "transport_error": "TimeoutError", "delay_seconds": 0,
        }])

    def test_certificate_error_is_not_transient(self):
        error = urllib.error.URLError(
            ssl.SSLCertVerificationError(1, "certificate verify failed")
        )
        self.assertFalse(transient_url_error(error))

    def test_fetch_page_sends_offset_and_detects_provider_end(self):
        feed = b'''<feed xmlns="http://www.w3.org/2005/Atom"></feed>'''
        with patch.object(self.source, "_request", return_value=feed) as request:
            result = self.source.fetch_page("llm", offset=30, page_size=10)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(request.call_args.args[0]).query)
        self.assertEqual(query["start"], ["30"])
        self.assertEqual(query["max_results"], ["10"])
        self.assertIsNone(result["next"])


if __name__ == "__main__":
    unittest.main()
