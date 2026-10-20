"""Exercise the actual isolated UI, state validator and merged service contract."""
from contextlib import closing
import hashlib
import http.client
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from scripts.mini_nonprod_fixture import create
from paper_agents.mini_nonprod_state import validate
from paper_agents.package_runtime import initialize, main, model_status
from paper_agents.runtime_config import migration_settings
from paper_agents.web import make_handler

ROOT = Path(__file__).resolve().parents[2]
ENV = {'PAPER_AGENT_STARTUP_MODE': 'serving', 'PAPER_AGENT_PACKAGED': '1',
       'PAPER_AGENT_SERVING_ONLY': '1', 'PAPER_NON_PRODUCTION': '1',
       'PAPER_AGENT_SCHEDULER_ENABLED': '0', 'PAPER_AGENT_GEMINI_ENABLED': '0',
       'PAPER_AGENT_TELEMETRY': '0', 'PAPER_AGENT_MINILM_ENABLED': '0',
       'PAPER_AGENT_SYNC_SHARED_SECRET': ''}


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / 'state'
        create(self.root)

    def test_validation_never_seeds_migrates_or_changes_database(self):
        db = self.root / 'data/paper_agent.db'
        before = hashlib.sha256(db.read_bytes()).hexdigest()
        with patch.dict(os.environ, ENV, clear=True):
            initialize(self.root)
        self.assertEqual(before, hashlib.sha256(db.read_bytes()).hexdigest())
        with closing(sqlite3.connect(db)) as c:
            c.execute('CREATE TABLE unexpected (id INTEGER)')
            c.commit()
        with self.assertRaisesRegex(RuntimeError, 'schema differs'):
            validate(self.root)
        db.unlink()
        with self.assertRaises(RuntimeError):
            validate(self.root)
        self.assertFalse(db.exists())

    def test_marker_and_alias_are_rejected(self):
        marker = self.root / 'data/MINI_NONPROD_SYNTHETIC'
        marker.unlink()
        with self.assertRaises(RuntimeError):
            validate(self.root)
        marker.write_text('synthetic-v1\n')
        profile = self.root / 'data/profile.json'
        source = self.root / 'profile.json'
        profile.rename(source)
        profile.symlink_to(source)
        with self.assertRaises(RuntimeError):
            validate(self.root)

    def test_unsafe_runtime_flags_are_rejected(self):
        for key, value in (('PAPER_NON_PRODUCTION', '0'),
                           ('PAPER_AGENT_SCHEDULER_ENABLED', '1'),
                           ('PAPER_AGENT_MINILM_ENABLED', '1'),
                           ('PAPER_AGENT_SYNC_SHARED_SECRET', 'test-secret')):
            with self.subTest(key=key), patch.dict(os.environ, {**ENV, key: value}, clear=True):
                with self.assertRaises(ValueError):
                    migration_settings()
        with patch.dict(os.environ, ENV, clear=True), patch('paper_agents.package_runtime.request_json') as call:
            self.assertEqual(model_status()['inference'], 'disabled')
            call.assert_not_called()

    def test_start_retains_lifecycle_lease_and_validates_before_ui(self):
        old = Path.cwd()
        try:
            os.chdir(self.root)
            with patch.dict(os.environ, {**ENV, 'PAPER_AGENT_LIFECYCLE_DIR': str(self.root/'control')}, clear=True), patch('sys.argv', ['runtime', 'start']), patch('paper_agents.web.run_review_ui') as ui:
                self.assertEqual(main(), 0)
                ui.assert_called_once()
                self.assertTrue((self.root/'control/.runtime.lock').is_file())
        finally:
            os.chdir(old)

    def test_real_http_ui_feedback_and_forbidden_execution_routes(self):
        old = Path.cwd()
        os.chdir(self.root)
        try:
            with patch.dict(os.environ, ENV, clear=True), patch('paper_agents.manual_scout.start') as scout, patch('paper_agents.web.suggest_topic_proposal') as provider:
                server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.root/'data/paper_agent.db'))
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                def request(path, body=None):
                    conn = http.client.HTTPConnection(*server.server_address, timeout=3)
                    try:
                        conn.request('GET' if body is None else 'POST', path, body,
                                     {'Content-Type': 'application/x-www-form-urlencoded'})
                        response = conn.getresponse()
                        return response.status, response.read().decode()
                    finally:
                        conn.close()
                try:
                    for path in ('/', '/topics?mode=agent', '/health'):
                        status, content = request(path)
                        self.assertEqual(status, 200, path)
                        self.assertIn('NON-PRODUCTION', content)
                    self.assertEqual(json.loads(request('/runtime')[1])['mode'], 'serving-only')
                    self.assertEqual(request('/scout/run', '')[0], 404)
                    self.assertEqual(request('/topics', 'action=propose&request_text=synthetic')[0], 503)
                    self.assertEqual(request('/topics', 'action=apply_proposal')[0], 503)
                    self.assertEqual(request('/sync/results', '{}')[0], 404)
                    self.assertEqual(request('/sync/input')[0], 404)
                    self.assertEqual(request('/feedback', 'paper_id=1&action=status&status=not_interested&notes=integration-persistence')[0], 303)
                    scout.assert_not_called()
                    provider.assert_not_called()
                finally:
                    server.shutdown()
                    thread.join(timeout=5)
                    server.server_close()
                initialize(self.root)
                with closing(sqlite3.connect(self.root/'data/paper_agent.db')) as c:
                    self.assertEqual(c.execute('SELECT notes FROM feedback WHERE paper_id=1 ORDER BY id DESC LIMIT 1').fetchone()[0], 'integration-persistence')
        finally:
            os.chdir(old)

    def test_compose_merge_preserves_isolation_and_keeps_pipeline_disabled(self):
        # Parser placeholders only. These are deliberately not deployable image identities.
        env = {**os.environ, 'PAPER_MINI_NONPROD_IMAGE': 'parser-only/app@sha256:'+'a'*64}
        result = subprocess.check_output(['docker', 'compose', '-f', str(ROOT/'deploy/mini-nonprod/compose.bootstrap.yml'),
            '-f', str(ROOT/'deploy/minilm/nonprod-client.override.yml'), 'config', '--format', 'json'], env=env, text=True)
        config = json.loads(result)
        app = config['services']['app']
        self.assertEqual(set(app['networks']), {'isolated', 'inference'})
        self.assertTrue(config['networks']['isolated']['internal'])
        self.assertTrue(config['networks']['inference']['external'])
        self.assertEqual(config['networks']['inference']['name'], 'paper-mini-inference')
        self.assertEqual(app['environment']['PAPER_AGENT_MINILM_URL'], 'http://paper-minilm:8091')
        self.assertEqual(app['environment']['PAPER_AGENT_MINILM_ENABLED'], '0')
        self.assertEqual(app['environment']['PAPER_AGENT_SCHEDULER_ENABLED'], '0')
        self.assertEqual(app['environment']['PAPER_AGENT_SERVING_ONLY'], '1')
        self.assertEqual(app['ports'][0]['host_ip'], '127.0.0.1')
        self.assertEqual(len(app['volumes']), 5)


if __name__ == '__main__':
    unittest.main()
