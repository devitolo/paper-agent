import hashlib
import re
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]


class PackageTests(unittest.TestCase):
    def test_archive_carries_complete_pipeline_and_verified_sources(self):
        workflow = (ROOT / '.github/workflows/observability-release.yml').read_text()
        line = next(line.strip() for line in workflow.splitlines() if line.strip().startswith('tar -czf '))
        members = line.split()[3:]
        required = {'deploy/observability/collect-publish.sh',
                    'deploy/observability/install_collect_publish.sh',
                    'deploy/observability/install_package.sh',
                    'deploy/observability/project-paper-observability-deploy'}
        self.assertTrue(required.issubset(members))
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'package.tar.gz'
            subprocess.run(['tar', '-czf', str(archive), *members], cwd=ROOT, check=True)
            with tarfile.open(archive) as package:
                installer = package.extractfile('deploy/observability/install_collect_publish.sh').read().decode()
                for name in ('collect-publish.sh', 'project-paper-observability-deploy'):
                    content = package.extractfile('deploy/observability/' + name).read()
                    digest = hashlib.sha256(content).hexdigest()
                    self.assertIn(digest, installer)

    def test_entrypoint_refuses_aws_without_side_effects(self):
        result = subprocess.run(['bash', str(ROOT / 'deploy/observability/install_package.sh'), '--target', 'aws-production'], capture_output=True)
        self.assertEqual(result.returncode, 64)


if __name__ == '__main__':
    unittest.main()
