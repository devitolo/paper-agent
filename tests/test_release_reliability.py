import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from paper_agents.scout import ArxivSource

ROOT = Path(__file__).resolve().parents[1]

class ReleaseReliabilityTests(unittest.TestCase):
    def test_retry_after_http_date(self):
        with patch('paper_agents.scout.time.time', return_value=0):
            self.assertEqual(ArxivSource()._retry_delay(0, 'Thu, 01 Jan 1970 00:02:00 GMT'), 120)

    def test_existing_install_is_never_cleaned_on_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            shutil.copy(ROOT / 'scripts/release_acceptance.sh', root / 'scripts')
            for name in ('.env', '.paper-install'):
                (root / name).write_text('existing customer state')
            env = dict(os.environ, PAPER_ACCEPTANCE_APP_IMAGE='example/app@sha256:' + 'a'*64,
                       PAPER_ACCEPTANCE_OLLAMA_IMAGE='example/model@sha256:' + 'b'*64)
            result = subprocess.run(['bash', str(root / 'scripts/release_acceptance.sh')], env=env, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            for name in ('.env', '.paper-install'):
                self.assertEqual((root / name).read_text(), 'existing customer state')
