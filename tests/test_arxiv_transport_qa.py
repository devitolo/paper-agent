"""Independent arXiv transport regressions; no source/network requests."""
import subprocess
import unittest
import urllib.error
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from paper_agents.scout import ArxivSource


class ArxivTransportIndependentTests(unittest.TestCase):
    def test_header_scratch_file_is_removed_on_success_http_error_and_timeout(self):
        for outcome in (200, 429, "timeout"):
            with self.subTest(outcome=outcome):
                paths = []
                def response(command, **kwargs):
                    path = Path(command[command.index("--dump-header") + 1])
                    paths.append(path)
                    self.assertTrue(path.exists())
                    path.write_bytes(b"HTTP/1.1 200 Connection established\r\n\r\n"
                                     b"HTTP/2 429\r\nretry-after: Thu, 01 Jan 1970 00:02:00 GMT\r\n\r\n")
                    if outcome == "timeout":
                        raise subprocess.TimeoutExpired(command, 5)
                    return subprocess.CompletedProcess(command, 0,
                        stdout=f"<feed/>\n__PROJECT_PAPER_HTTP_STATUS__={outcome}".encode(), stderr=b"")
                with patch("paper_agents.scout.subprocess.run", side_effect=response):
                    if outcome == "timeout":
                        with self.assertRaises(TimeoutError):
                            self.source()._request("https://example.test")
                    elif outcome == 429:
                        with self.assertRaises(urllib.error.HTTPError) as caught:
                            self.source()._request("https://example.test")
                        with patch("paper_agents.scout.time.time", return_value=0):
                            self.assertEqual(self.source()._retry_delay(0, caught.exception.headers.get("Retry-After")), 120)
                    else:
                        self.assertEqual(self.source()._request("https://example.test"), b"<feed/>")
                self.assertEqual(len(paths), 1)
                self.assertFalse(paths[0].exists())

    def source(self):
        source = ArxivSource(timeout=4, retries=1, request_delay=0, verbose=False)
        source.curl_path = "/usr/bin/curl"
        return source

    def test_curl_rate_limit_preserves_server_retry_after(self):
        def response(command, **kwargs):
            headers = b"HTTP/1.1 429 Too Many Requests\r\nRetry-After: 17\r\n\r\n"
            body = b"limited\n__PROJECT_PAPER_HTTP_STATUS__=429"
            # Model curl's header-output options: headers are otherwise discarded.
            for option in ("--dump-header", "-D"):
                if option in command:
                    destination = command[command.index(option) + 1]
                    if destination == "-":
                        body = headers + body
                    else:
                        Path(destination).write_bytes(headers)
            if "--include" in command or "-i" in command:
                body = headers + body
            return subprocess.CompletedProcess(command, 0, stdout=body, stderr=b"")
        with patch("paper_agents.scout.subprocess.run", side_effect=response), patch(
            "paper_agents.scout.time.sleep"
        ) as sleep:
            with self.assertRaises(urllib.error.HTTPError):
                self.source()._fetch_topic("operations", 4)
        self.assertIn(17.0, [call.args[0] for call in sleep.call_args_list])

    def test_subprocess_timeout_is_sanitized_and_retried_only_to_limit(self):
        with patch("paper_agents.scout.subprocess.run", side_effect=subprocess.TimeoutExpired(
            ["curl", "PRIVATE_TOKEN"], 5, stderr=b"PRIVATE_BODY"
        )) as run, patch("paper_agents.scout.time.sleep"):
            with self.assertRaises(TimeoutError) as caught:
                self.source()._fetch_topic("operations", 4)
        self.assertEqual(run.call_count, 2)
        self.assertNotIn("PRIVATE", str(caught.exception))

    def test_http_error_status_mapping(self):
        for status in (400, 403, 406, 429, 500, 503):
            with self.subTest(status=status), patch("paper_agents.scout.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0,
                    stdout=f"body\n__PROJECT_PAPER_HTTP_STATUS__={status}".encode(), stderr=b"")):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self.source()._request("https://example.test")
                self.assertEqual(caught.exception.code, status)

    def test_urllib_fallback_parses_feed_without_curl(self):
        with patch("paper_agents.scout.shutil.which", return_value=None):
            source = ArxivSource(retries=0, verbose=False)
        feed = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Test</title></entry></feed>'
        with patch("paper_agents.scout.urllib.request.urlopen", return_value=BytesIO(feed)), patch(
            "paper_agents.scout.subprocess.run"
        ) as run:
            self.assertEqual(len(source._fetch_topic("operations", 4)), 1)
        run.assert_not_called()

    def test_curl_ignores_user_config_that_can_enable_hidden_retries(self):
        with patch("paper_agents.scout.subprocess.run", return_value=subprocess.CompletedProcess(
            [], 0, stdout=b"<feed/>\n__PROJECT_PAPER_HTTP_STATUS__=200", stderr=b""
        )) as run:
            self.source()._request("https://example.test")
        self.assertIn(run.call_args.args[0][1], ("-q", "--disable"),
                      "disable curlrc before other arguments to keep retries and output deterministic")
