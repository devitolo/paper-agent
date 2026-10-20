"""Production worker lifecycle checks; no nonprod UI/inference exercises."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from paper_agents import package_runtime, pipeline_runtime


class PipelineRoleTests(unittest.TestCase):
    def test_pipeline_start_never_imports_web_and_keeps_initialization(self):
        import builtins
        original = builtins.__import__
        def guarded(name, *args, **kwargs):
            if name == 'paper_agents.web':
                raise AssertionError('pipeline must not import web')
            return original(name, *args, **kwargs)
        with patch.dict(os.environ, {'PAPER_AGENT_RUNTIME_ROLE': 'pipeline',
                'PAPER_AGENT_STARTUP_MODE': 'fresh', 'PAPER_AGENT_MINILM_BACKEND': 'embedded'}, clear=True), \
                patch('sys.argv', ['runtime', 'start']), \
                patch.object(package_runtime, 'initialize') as init, \
                patch.object(pipeline_runtime, 'run') as run, \
                patch('builtins.__import__', side_effect=guarded):
            self.assertEqual(package_runtime.main(), 0)
            init.assert_called_once()
            run.assert_called_once()

    def test_stale_process_identity_fails_before_database_read(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'ready.json'
            marker.write_text(json.dumps({'role': 'pipeline', 'pid': os.getpid(), 'process_start': 'old'}))
            with patch.dict(os.environ, {'PAPER_AGENT_RUNTIME_ROLE': 'pipeline'}), \
                    patch.object(pipeline_runtime, 'READY_FILE', marker), \
                    patch.object(pipeline_runtime, 'process_identity', return_value='new'), \
                    patch.object(package_runtime, 'app_status') as state:
                with self.assertRaisesRegex(RuntimeError, 'Stale'):
                    pipeline_runtime.readiness()
                state.assert_not_called()

    def test_ui_cannot_pass_pipeline_readiness(self):
        with patch.dict(os.environ, {'PAPER_AGENT_RUNTIME_ROLE': 'ui'}):
            with self.assertRaisesRegex(RuntimeError, 'requires pipeline'):
                pipeline_runtime.readiness()

    def test_readiness_requires_live_process_and_readable_database(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'ready.json'
            marker.write_text(json.dumps({'role': 'pipeline', 'pid': os.getpid(), 'process_start': 'same'}))
            with patch.dict(os.environ, {'PAPER_AGENT_RUNTIME_ROLE': 'pipeline'}), \
                    patch.object(pipeline_runtime, 'READY_FILE', marker), \
                    patch.object(pipeline_runtime, 'process_identity', return_value='same'), \
                    patch.object(package_runtime, 'app_status', return_value={'ready': False}) as state:
                with self.assertRaisesRegex(RuntimeError, 'database unavailable'):
                    pipeline_runtime.readiness()
                state.return_value = {'ready': True, 'database': 'readable'}
                self.assertTrue(pipeline_runtime.readiness()['ready'])

    @unittest.skipUnless(sys.platform == 'linux', 'production Linux process identity')
    def test_worker_has_no_socket_use_and_releases_lease_on_sigterm(self):
        from paper_agents.migration_lifecycle import lease
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / 'ready.json'
            code = '''
import socket, sys
from pathlib import Path
from paper_agents import pipeline_runtime as worker
from paper_agents.migration_lifecycle import lease
def forbidden(*a, **k): raise AssertionError('pipeline opened socket')
socket.socket = forbidden
worker.READY_FILE = Path(sys.argv[1])
with lease(Path(sys.argv[2])):
    worker.run()
'''
            process = subprocess.Popen([sys.executable, '-c', code, str(marker), str(root)],
                env={**os.environ, 'PAPER_AGENT_RUNTIME_ROLE': 'pipeline'})
            try:
                deadline = time.monotonic() + 5
                while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(marker.exists())
                with self.assertRaisesRegex(RuntimeError, 'busy'):
                    with lease(root, exclusive=True):
                        pass
                process.send_signal(signal.SIGTERM)
                self.assertEqual(process.wait(timeout=5), 0)
                self.assertFalse(marker.exists())
                with lease(root, exclusive=True):
                    pass
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()


if __name__ == '__main__':
    unittest.main()
