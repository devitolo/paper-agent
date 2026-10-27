"""Received Mini telemetry backend and rendered Health contracts."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from paper_agents import observability_transport as transport, web
from tests.test_observability_transport import fixture


class MiniTelemetryHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'mini-state.json'
        self.clock = 1793048400
        self.payload = fixture(self.clock)
        self.env = patch.dict(os.environ, {'PAPER_OBSERVABILITY_STATE_FILE': str(self.path),
                                          'PAPER_AGENT_MINI_RESOURCE_STATUS_FILE': '/never-read-mini-directly'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def save(self, payload=None):
        transport.ingest(self.path, transport.encoded(payload or self.payload), str(uuid.uuid4()), clock=self.clock)

    def render(self, clock=None):
        with patch.object(transport.time, 'time', return_value=self.clock if clock is None else clock), patch.object(web, 'sync_health_snapshot', return_value={'state': 'unknown', 'time': 'unknown', 'revision': 'unknown'}):
            return web.render_health_infrastructure({'db': {'integrity': 'ok', 'path': 'test.db'}})

    def test_received_state_uses_read_only_accessor_and_zero_metrics_render(self):
        self.save()
        before = self.path.read_bytes()
        status = transport.health_status(clock=self.clock)
        self.assertEqual(status['source'], 'Mini')
        self.assertEqual(status['freshness'], 'fresh')
        self.assertEqual(status['received_at'], self.payload['status']['observed_at'])
        html = self.render()
        for expected in ('Mini', 'just now', '0% average', '0% maximum observed',
                         '0.00 GiB available average', '50% used', 'complete · 15-minute window',
                         'last delivery accepted by AWS', web.telemetry_pacific_time(self.payload['status']['observed_at'])):
            self.assertIn(expected, html)
        self.assertNotIn('/never-read-mini-directly', html)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual([p.name for p in self.path.parent.iterdir() if p.name != 'mini-state.json.lock'], ['mini-state.json'])

    def test_resource_age_not_recent_receipt_controls_staleness(self):
        self.save()
        saved = json.loads(self.path.read_text())
        saved['received_at'] = fixture(self.clock + 2701)['status']['observed_at']
        self.path.write_text(json.dumps(saved))
        html = self.render(self.clock + 2701)
        self.assertIn('placeholder · stale', html)
        self.assertIn('No fresh Mini telemetry received within 45 minutes.', html)
        self.assertNotIn('50% used', html)
        self.assertNotIn('0% average', html)

    def test_pacific_display_handles_daylight_saving_and_invalid_times(self):
        self.assertEqual(web.telemetry_pacific_time('2026-10-26T21:45:06+00:00'), '2026-10-26 02:45:06 PM PDT')
        self.assertEqual(web.telemetry_pacific_time('2026-12-26T21:45:06Z'), '2026-12-26 01:45:06 PM PST')
        for value in (None, 'invalid', '2026-10-26T21:45:06'):
            self.assertEqual(web.telemetry_pacific_time(value), 'unknown')

    def test_timestamp_age_turns_red_only_after_45_minutes(self):
        observed = self.payload['status']['observed_at']
        for seconds, label, stale in ((0, 'just now', False), (60, '1 minute ago', False),
                                      (2700, '45 minutes ago', False), (2701, '45 minutes ago', True),
                                      (7200, '2 hours ago', True)):
            with self.subTest(seconds=seconds):
                html = web.render_telemetry_timestamp(observed, clock=self.clock + seconds)
                self.assertIn(label, html)
                self.assertEqual('class="telemetry-age telemetry-age-stale"' in html, stale)
        for value in (None, 'invalid', '<script>secret</script>'):
            self.assertEqual(web.render_telemetry_timestamp(value), 'unknown')

    def test_old_observation_is_red_even_when_receipt_is_recent(self):
        self.save()
        saved = json.loads(self.path.read_text())
        saved['received_at'] = fixture(self.clock + 2701)['status']['observed_at']
        self.path.write_text(json.dumps(saved))
        html = self.render(self.clock + 2701)
        self.assertNotIn('Telemetry Freshness</dt>', html)
        self.assertEqual(html.count('class="telemetry-age telemetry-age-stale"'), 1)
        self.assertIn('just now', html)
        self.assertIn('45 minutes ago', html)
        self.assertNotIn('50% used', html)

    def test_freshness_threshold(self):
        self.save()
        self.assertEqual(transport.health_status(clock=self.clock + 2700)['freshness'], 'fresh')
        self.assertEqual(transport.health_status(clock=self.clock + 2701)['freshness'], 'stale')

    def test_missing_state_does_not_create_file_or_lock(self):
        html = self.render()
        self.assertIn('No Mini telemetry has been received.', html)
        self.assertIn('placeholder · unavailable', html)
        self.assertEqual(list(self.path.parent.iterdir()), [])

    def test_malformed_sensitive_state_fails_closed(self):
        self.save()
        saved = json.loads(self.path.read_text())
        saved['payload']['status']['password'] = '<script>secret</script>'
        self.path.write_text(json.dumps(saved))
        html = self.render()
        self.assertIn('Stored Mini telemetry is invalid.', html)
        self.assertNotIn('<script>secret</script>', html)
        self.assertNotIn('50% used', html)

    def test_future_observation_and_receipt_fail_closed(self):
        self.save()
        html = self.render(self.clock - 301)
        self.assertIn('timestamps are inconsistent or in the future', html)
        self.assertNotIn('50% used', html)
        saved = json.loads(self.path.read_text())
        saved['received_at'] = fixture(self.clock + 301)['status']['observed_at']
        self.path.write_text(json.dumps(saved))
        self.assertEqual(transport.health_status(clock=self.clock)['freshness'], 'unavailable')

    def test_permission_failure_is_generic(self):
        with patch.object(transport, 'read_state', side_effect=PermissionError('/private/secret/path')):
            html = self.render()
        self.assertIn('Stored Mini telemetry cannot be read.', html)
        self.assertNotIn('/private/secret/path', html)

    def test_warming_and_absent_metrics_are_not_reported_complete(self):
        payload = copy.deepcopy(self.payload)
        payload['status'].update(state='warming', coverage_seconds=0, sample_count=0)
        payload['status']['window'].update(complete=False, sample_gaps=1)
        payload['status']['cpu'] = {key: None for key in transport.CPU}
        payload['status']['latest']['disk'] = None
        self.save(payload)
        html = self.render()
        self.assertIn('warming · 0/900 seconds covered · 1 gaps', html)
        self.assertIn('unavailable average', html)
        self.assertNotIn('complete · 15-minute window', html)
