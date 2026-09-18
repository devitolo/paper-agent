import json
import unittest
import urllib.error
import urllib.parse
from unittest.mock import patch

from paper_agents.scout import ArxivSource


class ArxivCooldownTests(unittest.TestCase):
    def test_406_switches_once_to_single_result_mode_for_remaining_topics(self):
        from io import BytesIO
        source = ArxivSource(retries=0, request_delay=0, verbose=False)
        error = urllib.error.HTTPError('https://example.test', 406, 'not acceptable', {}, None)
        feed = BytesIO(b'<feed xmlns="http://www.w3.org/2005/Atom"/>')
        later_feed = BytesIO(b'<feed xmlns="http://www.w3.org/2005/Atom"/>')

        with patch('paper_agents.scout.urllib.request.urlopen',
                   side_effect=[error, feed, later_feed]) as fetch:
            self.assertEqual(source.fetch(['one', 'two'], 8, 24), [])

        self.assertEqual(fetch.call_count, 3)
        requested = [urllib.parse.parse_qs(call.args[0].full_url.split('?', 1)[1])['max_results'][0]
                     for call in fetch.call_args_list]
        self.assertEqual(requested, ['4', '1', '1'])
        self.assertTrue(source.single_result_mode)
        self.assertTrue(source.last_diagnostics['coverage_reduced'])
        self.assertEqual(source.last_diagnostics['coverage_mode'], 'single_result_406_fallback')
        self.assertEqual([event['event'] for event in source.last_diagnostics['requests']].count('degraded_mode'), 1)

    def test_406_on_single_result_request_fails_without_recursion(self):
        source = ArxivSource(retries=0, verbose=False)
        source.single_result_mode = True
        error = urllib.error.HTTPError('https://example.test', 406, 'not acceptable', {}, None)
        with patch('paper_agents.scout.urllib.request.urlopen', side_effect=error) as fetch:
            with self.assertRaises(urllib.error.HTTPError):
                source._fetch_topic('one', 4)
        self.assertEqual(fetch.call_count, 1)

    def test_exhausted_429_stops_all_topics_and_later_fetches(self):
        source = ArxivSource(retries=1, verbose=False)
        error = urllib.error.HTTPError('https://example.test', 429, 'limited', {'Retry-After': '17'}, None)
        with patch('paper_agents.scout.urllib.request.urlopen', side_effect=error) as fetch, patch('paper_agents.scout.time.sleep') as sleep, patch('paper_agents.runtime_config.packaged', return_value=False):
            with self.assertRaisesRegex(RuntimeError, '1 failed, 0 successful.*2 skipped'):
                source.fetch(['one', 'two', 'three'], 20, 24)
            with self.assertRaisesRegex(RuntimeError, 'source cooldown'):
                source.fetch(['four'], 40, 24)
        self.assertEqual(fetch.call_count, 2)
        sleep.assert_called_once_with(17.0)
        self.assertTrue(source.last_diagnostics['cooldown_active'])
        self.assertEqual(source.last_diagnostics['cooldown_scope'], 'remainder_of_run')
        events = source.last_diagnostics['requests']
        self.assertTrue(all(event['at'].endswith('+00:00') for event in events))
        self.assertEqual([e['status'] for e in events if e['event'] == 'http_error'], [429, 429])
        json.dumps(source.last_diagnostics)

    def test_empty_success_is_not_reported_as_all_topics_failed(self):
        source = ArxivSource(retries=0, request_delay=0, verbose=False)
        error = urllib.error.HTTPError('https://example.test', 503, 'unavailable', {}, None)
        with patch.object(source, '_fetch_topic', side_effect=[error, []]):
            with self.assertRaisesRegex(RuntimeError, '1 failed, 1 successful \\(1 empty\\), 0 skipped'):
                source.fetch(['one', 'two'], 20, 24)
        self.assertFalse(source.cooldown_active)

    def test_successful_retry_does_not_trigger_cooldown(self):
        from io import BytesIO
        source = ArxivSource(retries=1, request_delay=0, verbose=False)
        error = urllib.error.HTTPError('https://example.test', 429, 'limited', {}, None)
        with patch('paper_agents.scout.urllib.request.urlopen', side_effect=[error, BytesIO(b'<feed/>'), BytesIO(b'<feed/>')]) as fetch, patch('paper_agents.scout.time.sleep'):
            self.assertEqual(source.fetch(['one', 'two'], 20, 24), [])
        self.assertEqual(fetch.call_count, 3)
        self.assertFalse(source.cooldown_active)
        self.assertEqual(source.last_diagnostics['successful_topics'], 2)

    def test_retry_after_http_date_is_respected(self):
        with patch('paper_agents.scout.time.time', return_value=0):
            self.assertEqual(ArxivSource()._retry_delay(0, 'Thu, 01 Jan 1970 00:02:00 GMT'), 120)


if __name__ == '__main__':
    unittest.main()
