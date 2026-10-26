"""Run only in an explicitly opted-in disposable Linux container, never a host."""
import os
from pathlib import Path
import subprocess
import sys
import unittest

ISOLATED = (sys.platform == 'linux' and os.environ.get('PAPER_OBSERVABILITY_QA_ISOLATED') == '1'
            and Path('/.dockerenv').exists() and Path('/qa-source/deploy/observability').is_dir())


@unittest.skipUnless(ISOLATED, 'Requires disposable Linux QA container')
class DeployPipelineTests(unittest.TestCase):
    def setUp(self):
        # /etc is an empty tmpfs in the test container.
        Path('/etc/passwd').write_text('root:x:0:0:root:/root:/bin/sh\n')
        Path('/etc/group').write_text('root:x:0:\n')
        Path('/usr/local/sbin').mkdir(parents=True, exist_ok=True)
        Path('/etc/cron.d').mkdir(parents=True, exist_ok=True)
        Path('/run/lock').mkdir(parents=True, exist_ok=True)
        self.pipeline = Path('/usr/local/sbin/project-paper-observability-collect-publish')
        self.pipeline.write_text('#!/bin/sh\necho unexpected_publication >&2\nexit 42\n')
        self.pipeline.chmod(0o755)
        launcher = Path('/usr/local/sbin/project-paper-observability-container')
        launcher.write_text('''#!/usr/bin/env python3
import datetime,json,os,pathlib
p=pathlib.Path(os.environ['PROJECT_PAPER_RESOURCE_EXPORT_DIR'])/'status.json'
p.write_text(json.dumps({'schema':'project-paper.resource-status.v1','scope':'host',
'environment':os.environ['PROJECT_PAPER_RESOURCE_ENVIRONMENT'],
'observed_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}))
''')
        launcher.chmod(0o755)

    def deploy(self, target):
        return subprocess.run(['bash', '/qa-source/deploy/observability/project-paper-observability-deploy',
                               '--image', 'ghcr.io/devitolo/paper-agent/observability:qa@sha256:'+'a'*64,
                               '--target', target], capture_output=True, text=True, timeout=10)

    def test_mini_image_update_preserves_pipeline_without_publishing(self):
        for _ in range(2):
            result = self.deploy('mini-production')
            self.assertEqual(result.returncode, 0, result.stderr)
            cron = Path('/etc/cron.d/project-paper-observability-mini-production').read_text()
            self.assertIn(str(self.pipeline), cron)
            self.assertEqual(cron.count('*/15'), 1)

    def test_aws_image_update_keeps_collector_only(self):
        result = self.deploy('aws-production')
        self.assertEqual(result.returncode, 0, result.stderr)
        cron = Path('/etc/cron.d/project-paper-observability-aws-production').read_text()
        self.assertNotIn(str(self.pipeline), cron)
        self.assertIn('/usr/local/sbin/project-paper-observability-container', cron)

    def test_untrusted_wrapper_refused(self):
        self.pipeline.chmod(0o777)
        result = self.deploy('mini-production')
        self.assertEqual(result.returncode, 75)

    def test_collection_only_host_keeps_existing_behavior(self):
        self.pipeline.unlink()
        result = self.deploy('mini-production')
        self.assertEqual(result.returncode, 0, result.stderr)
        cron = Path('/etc/cron.d/project-paper-observability-mini-production').read_text()
        self.assertNotIn(str(self.pipeline), cron)


if __name__ == '__main__':
    unittest.main()
