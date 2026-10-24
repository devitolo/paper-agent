import io
import json
from email.message import Message
import unittest
from unittest.mock import patch
import urllib.error

from paper_agents.scout import SemanticScholarSource


class Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.payload


def limited(retry_after="7"):
    headers = Message()
    headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(
        "https://api.semanticscholar.org/graph/v1/paper/search",
        429,
        "rate limited",
        headers,
        io.BytesIO(b'{"message":"limited"}'),
    )


def page(offset=0):
    return {"data": [], "offset": offset, "next": None}


class SemanticScholarKeyFailoverTests(unittest.TestCase):
    def source(self, retries=2):
        return SemanticScholarSource(
            api_keys=("key-one-secret", "key-two-secret"),
            retries=retries,
            request_delay=0,
            verbose=False,
        )

    def test_key1_success_never_uses_key2(self):
        source = self.source()
        requests = []

        def fetch(request, timeout):
            requests.append(request)
            return Response(page())

        with patch("paper_agents.scout.urllib.request.urlopen", fetch):
            source.fetch_page("incident", offset=0, cutoff="2025-01-01", page_size=10)
        self.assertEqual(len(requests), 1)
        self.assertEqual(dict(requests[0].header_items())["X-api-key"], "key-one-secret")
        self.assertEqual(source.diagnostics()["active_key_slot"], "key1")
        self.assertEqual(source.diagnostics()["failover_count"], 0)

    def test_key1_two_429s_fail_over_once_and_reuse_page_offset(self):
        source = self.source()
        requests = []
        outcomes = [limited(), limited(), Response(page(20))]

        def fetch(request, timeout):
            requests.append(request)
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with patch("paper_agents.scout.urllib.request.urlopen", fetch), patch(
            "paper_agents.scout.time.sleep"
        ) as sleep:
            source.fetch_page("incident", offset=20, cutoff="2025-01-01", page_size=10)
        self.assertEqual(len(requests), 3)
        self.assertEqual([request.full_url.split("offset=")[1].split("&")[0] for request in requests], ["20", "20", "20"])
        self.assertEqual([dict(request.header_items())["X-api-key"] for request in requests], [
            "key-one-secret", "key-one-secret", "key-two-secret"
        ])
        self.assertEqual(source.diagnostics()["active_key_slot"], "key2")
        self.assertEqual(source.diagnostics()["failover_count"], 1)
        self.assertEqual(sleep.call_count, 2)

    def test_key2_429_stops_without_further_retry(self):
        source = self.source()
        outcomes = [limited("3"), limited("5"), limited("9")]

        def fetch(request, timeout):
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with patch("paper_agents.scout.urllib.request.urlopen", fetch), patch(
            "paper_agents.scout.time.sleep"
        ):
            with self.assertRaises(urllib.error.HTTPError):
                source.fetch_page("incident", offset=0, cutoff="2025-01-01", page_size=10)
        self.assertEqual(len(outcomes), 0)
        self.assertTrue(source.cooldown_active)
        self.assertTrue(source.diagnostics()["key2_429_stop"])
        self.assertEqual(source.last_diagnostics["stop_reason"], "provider_rate_limited_key2")

    def test_key1_burst_window_expires_before_second_429(self):
        source = self.source()
        outcomes = [limited(), limited(), Response(page())]
        requests = []

        def fetch(request, timeout):
            requests.append(request)
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with patch("paper_agents.scout.urllib.request.urlopen", fetch), patch(
            "paper_agents.scout.time.monotonic", side_effect=[0, 601]
        ), patch("paper_agents.scout.time.sleep"):
            source.fetch_page("incident", offset=0, cutoff="2025-01-01", page_size=10)
        self.assertEqual([dict(request.header_items())["X-api-key"] for request in requests], [
            "key-one-secret", "key-one-secret", "key-one-secret"
        ])
        self.assertEqual(source.diagnostics()["failover_count"], 0)

    def test_non_429_error_does_not_switch_keys(self):
        source = self.source()
        error = urllib.error.HTTPError("url", 403, "forbidden", Message(), io.BytesIO())
        with patch("paper_agents.scout.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(urllib.error.HTTPError):
                source.fetch_page("incident", offset=0, cutoff="2025-01-01", page_size=10)
        self.assertEqual(source.diagnostics()["active_key_slot"], "key1")
        self.assertEqual(source.diagnostics()["failover_count"], 0)

    def test_configuration_requires_two_distinct_keys_when_packaged(self):
        with patch.dict("os.environ", {"PAPER_AGENT_PACKAGED": "1", "SEMANTIC_SCHOLAR_API_KEY1": "only"}, clear=True):
            with self.assertRaisesRegex(ValueError, "two distinct"):
                SemanticScholarSource(verbose=False)


if __name__ == "__main__":
    unittest.main()
