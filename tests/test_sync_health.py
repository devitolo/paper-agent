import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from paper_agents.web import render_sync_health, sync_health_snapshot

class SyncHealthTests(unittest.TestCase):
    def test_success_and_html_escape(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'status.json'
            p.write_text(json.dumps({'updated_at':'2099-01-01T00:00:00+00:00','input_revision':'<secret>','overall_result':'completed','reason':'ok','snapshot_size':'12 MiB'}))
            with patch.dict(os.environ, {'PAPER_AGENT_ORCHESTRATOR_STATUS_FILE':str(p)}):
                html = render_sync_health()
                self.assertIn('Mini sync', html); self.assertIn('&lt;secret&gt;', html)
    def test_missing_malformed_and_stale_are_safe(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'status.json'
            with patch.dict(os.environ, {'PAPER_AGENT_ORCHESTRATOR_STATUS_FILE':str(p)}):
                self.assertEqual(sync_health_snapshot()['state'], 'unavailable')
                p.write_text('{bad')
                self.assertEqual(sync_health_snapshot()['state'], 'unavailable')
                p.write_text(json.dumps({'updated_at':'2020-01-01T00:00:00+00:00','input_revision':'r','overall_result':'completed'}))
                self.assertEqual(sync_health_snapshot()['state'], 'stale')

